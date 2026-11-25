"""Offline Gmail MIME, pagination, and ingest transaction checks."""

import base64
from contextlib import closing
from email.utils import formatdate
import sqlite3
import threading
import unittest
from unittest.mock import patch

import httpx

from c2_acceptance_check import Base, FakeGmail, ME, NOW, PLANTED, message
from jarvis.v3.index import Index
from jarvis.v3.ingest import Doc, RateLimited, Runner
from jarvis.v3.ingest.gmail import GmailError, GmailIngester, HistoryExpired, HttpGmail


def part(kind, raw, *, charset=None):
    headers = [] if charset is None else [{"name": "Content-Type", "value": f'{kind}; charset="{charset}"'}]
    return {"mimeType": kind, "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(raw).decode().rstrip("=")}}


def event(kind, mid):
    return {kind: [{"message": {"id": mid}}]}


class Mail(Base):
    def gmail(self, api, **kwargs):
        return GmailIngester(api, clock=lambda: self.now, **kwargs)

    def stored(self, ref):
        row = next(doc for doc in self.index.recent(20) if doc["ref"] == ref)
        result = self.index.read(row["id"])
        return result, "".join(chunk["text"] for chunk in result["messages"])

    def test_history_pages_and_add_then_delete(self):
        class Paged(FakeGmail):
            def history(self, start_history_id, page_token=None):
                self.calls.append(("history", start_history_id, page_token))
                if start_history_id == "106":
                    return {"historyId": "106"}
                if page_token is None:
                    return {"history": [event("messagesAdded", "transient")],
                            "historyId": "105", "nextPageToken": "second"}
                return {"history": [event("messagesDeleted", "transient"), event("messagesAdded", "kept")],
                        "historyId": "106"}

        api = Paged([message("transient", "Old", "Transient mail")])
        runner = self.runner(self.gmail(api))
        runner.tick()
        api.box["kept"] = message("kept", "New", "Retained mail")
        api.calls.clear()
        self.now += 300
        result = runner.tick()["gmail"]
        self.assertEqual((result["docs"], result["deleted"]), (1, 1))
        self.assertEqual([doc["ref"] for doc in self.index.recent(20)], ["kept"])
        self.assertNotIn(("get", "transient"), api.calls)
        self.now += 300
        runner.tick()
        self.assertEqual([call for call in api.calls if call[0] == "history"],
                         [("history", "100", None), ("history", "100", "second"), ("history", "106", None)])

    def test_label_change_replaces_or_removes_existing_mail(self):
        record = message("label", "Subject", "archivable word")
        api = FakeGmail([record])
        runner = self.runner(self.gmail(api))
        runner.tick()
        record["labelIds"] = ["STARRED", "SENT"]
        update = {"history": [event("labelsAdded", "label")], "historyId": "101"}
        self.now += 300
        with patch.object(api, "history", return_value=update):
            runner.tick()
        self.assertEqual(self.stored("label")[0]["messages"][0]["role"], "owner")
        self.assertEqual(len(self.stored("label")[0]["messages"]), 1)
        record["labelIds"] = ["TRASH"]
        self.now += 300
        with patch.object(api, "history", return_value={"history": [event("labelsAdded", "label")], "historyId": "102"}):
            runner.tick()
        self.assertEqual(self.hits("archivable"), [])

    def test_history_checkpoints_an_oversized_final_page(self):
        api = FakeGmail()
        self.runner(self.gmail(api, page_docs=2)).tick()
        for number in range(5):
            api.add(message(f"m{number}", "Subject", "Under the history budget"))
        api.calls.clear()
        for expected in (2, 4, 5):
            with Index(self.index.path) as index:
                result = Runner(index, [self.gmail(api, page_docs=2)], clock=lambda: self.now).tick()["gmail"]
            self.assertIsNone(result["error"])
            self.assertEqual(len(self.index.recent(20)), expected)
        self.assertEqual([call for call in api.calls if call[0] == "history"], [("history", "100", None)])

    def test_mail_redacts_normalized_and_wrapped_dash_tokens(self):
        wide = "".join(chr(ord(char) + 0xFEE0) for char in PLANTED)
        wrapped = "\n".join((PLANTED[:20], PLANTED[20:]))
        record = message("linked", "archive－" + wide + ".txt", "Use backup-" + wrapped)
        self.runner(self.gmail(FakeGmail([record]))).tick()
        doc, text = self.stored("linked")
        self.assertNotIn(wide, doc["title"])
        self.assertNotIn(wrapped, text)
        self.assertIn("Use backup-[withheld: GitHub token]", text)

    def test_missing_fields_and_bad_base64(self):
        empty = message("empty", "", "")
        del empty["payload"]
        bad = message("bad", "", "")
        bad["payload"] = {"mimeType": "text/plain", "body": {"data": "???%"}}
        no_body = message("no_body", "", "")
        no_body["payload"] = {"mimeType": "text/plain"}
        self.drain(self.runner(self.gmail(FakeGmail([empty, bad, no_body]))), 3)
        for mid in ("empty", "bad", "no_body"):
            _, text = self.stored(mid)
            self.assertEqual(text, "From: \nTo: \nDate: ")

    def test_huge_body_is_bounded(self):
        record = message("huge", "Large", "leadwalrus " + "a" * 1_000_000 + " tailquokka")
        self.runner(self.gmail(FakeGmail([record]))).tick()
        _, text = self.stored("huge")
        self.assertIn("leadwalrus", text)
        self.assertNotIn("tailquokka", text)
        self.assertLess(len(text), 17_000)

    def test_nested_mime_plain_wins_and_images_are_never_decoded(self):
        record = message("nested", "=?iso-8859-1?Q?caf=E9?=", "", sender=f"Owner <{ME.upper()}>")
        record["payload"]["parts"] = [
            {"mimeType": "multipart/related", "parts": [
                {"mimeType": "multipart/alternative", "parts": [
                    None,
                    part("text/html", b"<p>htmlalternative</p>"),
                    part("text/plain", "Crème brûlée".encode("iso-8859-1"), charset="iso-8859-1"),
                ]},
                part("image/png", b"inlineimagebytes"),
            ]},
            {**part("text/plain", b"attachmentbytes"), "filename": "=?UTF-8?Q?r=C3=A9sum=C3=A9.txt?="},
        ]
        self.runner(self.gmail(FakeGmail([record]))).tick()
        doc, text = self.stored("nested")
        self.assertEqual(doc["title"], "café")
        self.assertEqual(doc["messages"][0]["role"], "owner")
        self.assertIn("Crème brûlée", text)
        self.assertIn("Attachments: résumé.txt", text)
        for gone in ("htmlalternative", "inlineimagebytes", "attachmentbytes"):
            self.assertNotIn(gone, text)

    def test_html_only_mail_keeps_text_without_active_or_quoted_content(self):
        record = message("html", "HTML", "")
        record["payload"] = part("text/html", b'<style>cssword</style><p>Caf\xe9 &amp; tea</p>'
                                 b'<a href="https://example.org">linked text</a>'
                                 b'<img src="https://example.com/inline.png">'
                                 b'<script>scriptword</script><p hidden>hiddenword</p>'
                                 b'<div class="gmail_quote"><p>quotedword</p></div>', charset="windows-1252")
        self.runner(self.gmail(FakeGmail([record]))).tick()
        _, text = self.stored("html")
        self.assertIn("Café & tea", text)
        self.assertIn("linked text", text)
        for gone in ("cssword", "scriptword", "quotedword", "hiddenword", "https://", "<p>"):
            self.assertNotIn(gone, text)

    def test_backfill_buffers_server_page_and_recovers_wrong_cursor(self):
        api = FakeGmail([message(f"m{i}", "", f"body {i}", age_days=i + 1) for i in range(3)])
        api.page = 3
        with self.index.write_transaction() as db:
            self.index.save_ingest_cursor(db, "gmail", {"history": "100", "cutoff": [], "pending": 3})
        runner = self.runner(self.gmail(api, page_docs=1))
        for count in range(1, 4):
            self.assertIsNone(runner.tick()["gmail"]["error"])
            self.assertEqual(len(self.index.recent(20)), count)
            self.now += 300
        runner.tick()
        lists = [call for call in api.calls if call[0] == "list"]
        self.assertEqual(len(lists), 1)
        self.assertEqual([call for call in api.calls if call[0] == "history"], [("history", "100", None)])


class Transactions(Base):
    def test_two_runners_discard_the_stale_poll(self):
        slow_started = threading.Event()
        release = threading.Event()

        class Poll:
            name = "notes"

            def __init__(self, value):
                self.value = value
                self.cursors = []

            def poll(self, cursor):
                self.cursors.append(cursor)
                if self.value == "stale":
                    slow_started.set()
                    if not release.wait(5):
                        raise TimeoutError
                return [Doc("same", "", NOW, [("sender", self.value, None)])], {"value": self.value}

        stale, fresh = Poll("stale"), Poll("fresh")
        results = []
        with Index(self.index.path) as other:
            slow_runner = Runner(other, [stale], clock=lambda: self.now)
            worker = threading.Thread(target=lambda: results.append(slow_runner.tick()))
            worker.start()
            try:
                self.assertTrue(slow_started.wait(5))
                self.assertEqual(self.runner(fresh).tick()["notes"]["docs"], 1)
            finally:
                release.set()
                worker.join(6)
        self.assertEqual(stale.cursors, fresh.cursors)
        self.assertEqual(results[0]["notes"]["docs"], 0)
        self.assertEqual(self.hits("stale"), [])
        self.assertEqual(len(self.hits("fresh")), 1)
        self.assertEqual(self.index.get_ingest_cursor("notes"), (1, {"value": "fresh"}))

    def test_cursor_failure_rolls_back_documents(self):
        api = FakeGmail([message("atomic", "Subject", "atomicword")])
        with self.index.write_transaction() as db:
            db.execute("""CREATE TRIGGER stop_cursor BEFORE INSERT ON ingest_cursors
                          BEGIN SELECT RAISE(ABORT, 'synthetic cursor fault'); END""")
        out = self.runner(GmailIngester(api, clock=lambda: self.now)).tick()
        self.assertTrue(out["gmail"]["error"])
        self.assertEqual(self.index.recent(20), [])
        self.assertEqual(self.index.get_ingest_cursor("gmail"), (0, None))

    def test_document_failure_rolls_back_cursor(self):
        api = FakeGmail([message("atomic", "Subject", "atomicword")])
        with self.index.write_transaction() as db:
            db.execute("""CREATE TRIGGER stop_doc BEFORE INSERT ON docs
                          BEGIN SELECT RAISE(ABORT, 'synthetic document fault'); END""")
        out = self.runner(GmailIngester(api, clock=lambda: self.now)).tick()
        self.assertTrue(out["gmail"]["error"])
        self.assertEqual(self.index.get_ingest_cursor("gmail"), (0, None))

    def test_additive_schema_preserves_existing_index(self):
        self.index.add("claude", "sample", title="Sample", ts=NOW, messages=[("user", "existingword", NOW)])
        with closing(sqlite3.connect(self.index.path, isolation_level=None)) as db:
            version = db.execute("PRAGMA user_version").fetchone()
            db.execute("DROP TABLE ingest_cursors")
        with Index(self.index.path) as reopened:
            self.assertEqual(len(reopened.discover("existingword")), 1)
            self.assertEqual(reopened.get_ingest_cursor("gmail"), (0, None))
        with closing(sqlite3.connect(self.index.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), version)


class Transport(unittest.TestCase):
    def client(self, *responses):
        requests = []
        pending = iter(responses)

        def reply(request):
            requests.append(request)
            return next(pending)

        client = HttpGmail(token=lambda: "fake-token", transport=httpx.MockTransport(reply), timeout=7)
        self.addCleanup(client.close)
        return client, requests

    def test_paths_params_timeout_and_get_only(self):
        client, requests = self.client(*(httpx.Response(200, json={}) for _ in range(4)))
        client.profile()
        client.list("after:123", "page/one")
        client.get("mail_id")
        client.history("123", "page/two")
        self.assertEqual([req.url.path for req in requests],
                         [f"/gmail/v1/users/me/{p}" for p in ("profile", "messages", "messages/mail_id", "history")])
        for req in requests:
            self.assertEqual((req.method, req.url.scheme, req.url.host), ("GET", "https", "gmail.googleapis.com"))
            self.assertEqual(req.headers["Authorization"], "Bearer fake-token")
            self.assertEqual(req.extensions["timeout"]["read"], 7)
        self.assertEqual(dict(requests[1].url.params), {"q": "after:123", "pageToken": "page/one"})
        self.assertEqual(dict(requests[2].url.params), {"format": "full"})
        self.assertEqual(dict(requests[3].url.params), {"startHistoryId": "123", "pageToken": "page/two"})

    def test_status_mapping_and_safe_errors(self):
        client, requests = self.client(
            httpx.Response(429, text="privatebody", headers={"Retry-After": "30"}),
            httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}},
                           headers={"Retry-After": formatdate(NOW + 45, usegmt=True)}),
            httpx.Response(404), httpx.Response(404),
            httpx.Response(403, json={"error": {"message": "privatebody", "errors": [{"reason": "forbidden"}]}}),
            httpx.Response(200, text="privatebody"), httpx.Response(503, text="privatebody"),
            httpx.Response(302, headers={"Location": "https://example.org"}),
        )
        with self.assertRaises(RateLimited) as limited:
            client.profile()
        self.assertEqual(limited.exception.retry_after, 30)
        with patch("jarvis.v3.ingest.gmail.time.time", return_value=NOW):
            with self.assertRaises(RateLimited) as limited:
                client.list("after:123")
        self.assertEqual(limited.exception.retry_after, 45)
        with self.assertRaises(HistoryExpired):
            client.history("123")
        self.assertIsNone(client.get("gone"))
        for _ in range(4):
            with self.assertRaises(GmailError) as failed:
                client.profile()
            self.assertNotIn("privatebody", str(failed.exception))
            self.assertIsNone(failed.exception.__context__)
        self.assertEqual(len(requests), 8)
        self.assertTrue(all(req.method == "GET" for req in requests))

    def test_transport_failure_has_no_secret_context(self):
        def unavailable(request):
            raise httpx.ReadTimeout("fake-token privatebody", request=request)

        client = HttpGmail(token=lambda: "fake-token", transport=httpx.MockTransport(unavailable))
        self.addCleanup(client.close)
        with self.assertRaises(GmailError) as failed:
            client.profile()
        self.assertNotIn("fake-token", str(failed.exception))
        self.assertNotIn("privatebody", str(failed.exception))
        self.assertIsNone(failed.exception.__context__)


if __name__ == "__main__":
    unittest.main()
