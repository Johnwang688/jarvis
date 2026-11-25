"""Orchestrator-owned acceptance tests for C2 (implementers: do not edit; if one is wrong, stop and say why).

Contract
  jarvis/v3/ingest/__init__.py      (stdlib only at import; importing it loads no Google, HTTP or model code)
    Doc(ref, title, ts, messages, meta=None, deleted=False)     messages: [(role, text, ts|None)]; deleted=True removes the doc (source, ref)
    RateLimited(retry_after=None)                                an ingester raises it; any other exception is an error
    Ingester: .name (the index `source`), .poll(cursor) -> (docs, cursor)    cursor: JSON-able dict, None on the first call
    Runner(index, ingesters, *, clock=time.time, interval_s=300, max_backoff_s=3600)
      tick() -> {name: {"docs": int, "deleted": int, "error": str|None, "next": epoch}}   only ingesters that were due appear
      Docs and the new cursor land in ONE index transaction; a doc that was indexed before is replaced, never appended to.
      After an error or RateLimited nothing of that poll is kept, the cursor is unchanged, and the wait doubles from
      interval_s up to max_backoff_s (RateLimited.retry_after, when larger, wins; capped too). One ingester failing never stops another.
      Error text is a short class of failure, never the exception's own text (it can carry mail).
  jarvis/v3/ingest/gmail.py
    GmailIngester(api, *, backfill_days=180, clock=time.time, page_docs=50)     name = "gmail"
      api.profile() -> {"emailAddress", "historyId"}
      api.list(query, page_token) -> {"messages": [{"id"}], "nextPageToken"?}          users.messages.list
      api.get(id) -> users.messages.get(format="full") resource, or None when it is gone
      api.history(start_history_id, page_token) -> {"history": [...], "historyId", "nextPageToken"?}; raises HistoryExpired
    First poll: backfill `after:<epoch now - backfill_days>` newest first, page_docs per poll, then history from the profile's
    historyId read BEFORE the backfill began. Later polls: messagesAdded -> docs, messagesDeleted -> Doc(deleted=True).
    HistoryExpired -> a new bounded backfill. Trash, spam, drafts and chats are not indexed.
    Doc: ref = message id; title = subject; ts = internalDate; one message, role "owner" for sent mail (label SENT or From is
    the profile address) else "sender"; text = "From/To/Date" lines, then the plain body with quoted replies and the quoted
    header line ("On ... wrote:") cut, then "Attachments: a.pdf, b.png" (names only, never content).
    meta = {"untrusted": 1, "from_owner": 0|1, "thread": threadId, "from": ..., "to": ..., "labels": [...]}
    HttpGmail(token=google_auth.access_token) is the real api (httpx, read-only GETs); it is imported lazily and never in tests.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis.v3 import ingest
from jarvis.v3.index import Index
from jarvis.v3.ingest import Doc, RateLimited, Runner
from jarvis.v3.ingest import gmail
from jarvis.v3.ingest.gmail import GmailIngester, HistoryExpired

NOW = 1_800_000_000.0
PLANTED = "ghp" + "_" + "C2x9" * 9
ME = "owner@example.com"


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(mid, subject, body, *, sender="Ada <ada@example.org>", to=ME, labels=("INBOX",), age_days=1, files=(), html=None):
    parts = [{"mimeType": "text/plain", "body": {"data": b64(body)}}]
    if html:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    for name in files:
        parts.append({"mimeType": "application/pdf", "filename": name, "body": {"attachmentId": "att-" + name, "size": 10}})
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "internalDate": str(int((NOW - age_days * 86400) * 1000)),
            "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": sender}, {"name": "To", "value": to},
                                                                  {"name": "Subject", "value": subject}, {"name": "Date", "value": "Tue, 1 Jan 2030 10:00:00 +0000"}],
                        "parts": parts}}


class FakeGmail:
    def __init__(self, messages=()):
        self.box = {m["id"]: m for m in messages}
        self.history_id = 100
        self.events = []            # (history id, kind, message id)
        self.calls = []
        self.fail = None
        self.page = 2
        self.gone = set()           # listed, but get() finds nothing
        self.expired_before = 0     # history ids below this are too old

    def profile(self):
        self.calls.append(("profile",))
        return {"emailAddress": ME, "historyId": str(self.history_id)}

    def list(self, query, page_token=None):
        self.calls.append(("list", query, page_token))
        if self.fail:
            raise self.fail
        ids = sorted(self.box, key=lambda i: -int(self.box[i]["internalDate"]))
        start = int(page_token or 0)
        out = {"messages": [{"id": i} for i in ids[start:start + self.page]]}
        if start + self.page < len(ids):
            out["nextPageToken"] = str(start + self.page)
        return out

    def get(self, mid):
        self.calls.append(("get", mid))
        return None if mid in self.gone else self.box.get(mid)

    def history(self, start_history_id, page_token=None):
        self.calls.append(("history", str(start_history_id), page_token))
        if self.fail:
            raise self.fail
        if int(start_history_id) < self.expired_before:
            raise HistoryExpired()
        rows = [{"id": str(h), "messagesAdded" if kind == "add" else "messagesDeleted": [{"message": {"id": mid}}]}
                for h, kind, mid in self.events if h > int(start_history_id)]
        return {"history": rows, "historyId": str(self.history_id)}

    def add(self, m):
        self.box[m["id"]] = m
        self.history_id += 1
        self.events.append((self.history_id, "add", m["id"]))

    def delete(self, mid):
        self.box.pop(mid, None)
        self.history_id += 1
        self.events.append((self.history_id, "del", mid))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.index = self.enterContext(Index(self.tmp / "v3" / "index.sqlite"))
        self.now = NOW

    def runner(self, *ingesters, **kw):
        return Runner(self.index, list(ingesters), clock=lambda: self.now, **kw)

    def drain(self, runner, rounds=12):
        for _ in range(rounds):
            runner.tick()
            self.now += 301

    def hits(self, query):
        return self.index.discover(query, limit=20)

    def texts(self, query):
        return "\n".join(m["text"] for hit in self.hits(query) for m in hit["messages"])


class Scripted:
    name = "notes"

    def __init__(self, *steps):
        self.steps, self.cursors = list(steps), []

    def poll(self, cursor):
        self.cursors.append(cursor)
        step = self.steps.pop(0) if self.steps else ([], cursor)
        if isinstance(step, BaseException):
            raise step
        return step


def note(ref, text, **kw):
    return Doc(ref=ref, title="Note " + ref, ts=NOW, messages=[("sender", text, NOW)], meta={"untrusted": 1}, **kw)


class Framework(Base):
    def test_docs_and_cursor_persist(self):
        a = Scripted(([note("1", "alpha walrus")], {"at": 1}), ([note("2", "beta heron")], {"at": 2}))
        r = self.runner(a)
        out = r.tick()
        self.assertEqual((out["notes"]["docs"], out["notes"]["error"]), (1, None))
        self.assertEqual(r.tick(), {})                         # not due yet
        self.now += 300
        r.tick()
        self.assertEqual(a.cursors, [None, {"at": 1}])
        self.assertEqual(self.hits("walrus")[0]["source"], "notes")
        b = Scripted()                                          # a new process continues from the stored cursor
        self.runner(b).tick()
        self.assertEqual(b.cursors, [{"at": 2}])

    def test_a_doc_seen_again_is_replaced_and_a_deleted_one_goes(self):
        a = Scripted(([note("1", "alpha walrus")], {"at": 1}), ([note("1", "alpha walrus again")], {"at": 2}), ([note("1", "", deleted=True)], {"at": 3}))
        r = self.runner(a)
        r.tick()
        self.now += 300
        r.tick()
        hit = self.hits("walrus")
        self.assertEqual(len(hit), 1)
        self.assertEqual(len(self.index.read(hit[0]["id"])["messages"]), 1)
        self.now += 300
        out = r.tick()
        self.assertEqual(out["notes"]["deleted"], 1)
        self.assertEqual(self.hits("walrus"), [])
        self.assertEqual(self.index.recent(5), [])

    def test_errors_back_off_and_keep_nothing(self):
        boom = RuntimeError("Subject: private words " + PLANTED)
        a = Scripted(boom, boom, RateLimited(retry_after=5000), RateLimited(retry_after=10), ([note("1", "alpha walrus")], {"at": 1}))
        a.name = "notes"
        ok = Scripted(([note("9", "gamma otter")], {"at": 1}))
        ok.name = "other"
        r = self.runner(a, ok, max_backoff_s=1200)
        out = r.tick()
        self.assertEqual(out["other"]["docs"], 1)               # one failing ingester does not stop another
        self.assertTrue(out["notes"]["error"])
        self.assertNotIn("private", json.dumps(out))
        self.assertNotIn(PLANTED, json.dumps(out))
        waits = []
        for _ in range(4):
            waits.append(out["notes"]["next"] - self.now)
            self.now = out["notes"]["next"]
            out = r.tick()
        self.assertEqual(waits[:2], [300, 600])
        self.assertEqual(waits[2], 1200)                        # retry_after 5000 is capped
        self.assertEqual(waits[3], 1200)                        # still backing off: doubling passed the cap
        self.assertEqual(out["notes"]["docs"], 1)
        self.assertEqual(a.cursors, [None] * 5)
        self.assertEqual(out["notes"]["next"] - self.now, 300)  # success resets the wait

    def test_a_poll_that_fails_midway_leaves_no_half(self):
        class Half:
            name = "half"

            def poll(self, cursor):
                return [note("1", "alpha walrus"), "not a doc"], {"at": 1}
        r = self.runner(Half())
        self.assertTrue(r.tick()["half"]["error"])
        self.assertEqual(self.hits("walrus"), [])

    def test_import_is_light(self):
        self.assertNotIn("httpx", Path(ingest.__file__).read_text())
        source = Path(gmail.__file__).read_text()
        self.assertNotIn("gmail.send", source)
        self.assertNotIn("gmail.modify", source)
        for verb in ("post(", "put(", "delete(", "patch("):
            self.assertNotIn("httpx." + verb, source)


class Gmail(Base):
    def box(self, n=5):
        return FakeGmail([message(f"m{i}", f"Subject {i}", f"Body number{i} words", age_days=i + 1) for i in range(n)])

    def test_backfill_then_history(self):
        api = self.box(5)
        g = GmailIngester(api, clock=lambda: self.now, page_docs=2)
        r = self.runner(g)
        r.tick()
        self.assertEqual(len(self.index.recent(20)), 2)         # bounded per poll
        self.assertEqual(api.calls[0], ("profile",))            # the history id is read before the backfill
        api.add(message("late", "Arrived during backfill", "Body latecomer words", age_days=0))
        self.drain(r)
        self.assertEqual(len(self.index.recent(20)), 6)
        query = next(c for c in api.calls if c[0] == "list")[1]
        self.assertIn(f"after:{int(NOW - 180 * 86400)}", query)
        self.assertEqual(len(self.hits("latecomer")), 1)
        calls = len(api.calls)
        api.add(message("m9", "Fresh", "Body newest heron"))
        api.delete("m1")
        self.drain(r, 2)
        self.assertEqual(len(self.hits("heron")), 1)
        self.assertEqual(self.hits("number1"), [])
        self.assertFalse([c for c in api.calls[calls:] if c[0] == "list"])   # incremental: no second backfill
        doc = self.hits("heron")[0]
        self.assertEqual((doc["source"], doc["ref"], doc["title"]), ("gmail", "m9", "Fresh"))

    def test_backfill_window_is_configurable_and_bounded(self):
        api = FakeGmail([message("old", "Old", "Body ancient words", age_days=400), message("new", "New", "Body recent words", age_days=3)])
        r = self.runner(GmailIngester(api, clock=lambda: self.now, backfill_days=30))
        self.drain(r, 3)
        self.assertIn(f"after:{int(NOW - 30 * 86400)}", next(c for c in api.calls if c[0] == "list")[1])
        self.assertEqual(self.hits("ancient"), [])              # a server that ignores the query is still bounded by date
        self.assertEqual(len(self.hits("recent")), 1)

    def test_sent_mail_is_from_owner_and_bodies_are_untrusted(self):
        api = FakeGmail([message("s1", "Re: lunch", "Sounds good, noon works.", sender=f"Owner <{ME}>", to="ada@example.org", labels=("SENT",)),
                         message("r1", "lunch", "Are you free at noon tomorrow?")])
        self.drain(self.runner(GmailIngester(api, clock=lambda: self.now)), 3)
        metas = {d["ref"]: json.loads(d["meta_json"]) for d in self.index.recent(5)}
        self.assertEqual((metas["s1"]["from_owner"], metas["r1"]["from_owner"]), (1, 0))
        self.assertEqual((metas["s1"]["untrusted"], metas["r1"]["untrusted"]), (1, 1))
        self.assertEqual(metas["r1"]["thread"], "t-r1")
        roles = {d["ref"]: self.index.read(d["id"])["messages"][0]["role"] for d in self.index.recent(5)}
        self.assertEqual(roles, {"s1": "owner", "r1": "sender"})

    def test_quoted_replies_are_cut_and_attachments_are_names(self):
        body = ("Thanks, that plan works.\n\nOn Mon, 1 Jan 2030 at 09:00, Ada <ada@example.org> wrote:\n> Earlier quotedsecret text\n> more\n>> deeper\n"
                "\n-----Original Message-----\nFrom: someone\nforwardedblock\n")
        api = FakeGmail([message("q1", "Re: plan", body, files=("budget.pdf", "site map.png"))])
        self.drain(self.runner(GmailIngester(api, clock=lambda: self.now)), 3)
        text = self.texts("plan works")
        self.assertIn("Thanks, that plan works.", text)
        for gone in ("quotedsecret", "deeper", "forwardedblock", "wrote:", "att-budget"):
            self.assertNotIn(gone, text)
        self.assertIn("budget.pdf", text)
        self.assertIn("site map.png", text)
        self.assertFalse([c for c in api.calls if "attachment" in str(c).lower()])

    def test_planted_credentials_are_redacted(self):
        api = FakeGmail([message("c1", "token " + PLANTED, "Here is the key " + PLANTED + " for the deploy walrus.", files=(PLANTED + ".txt",))])
        self.drain(self.runner(GmailIngester(api, clock=lambda: self.now)), 3)
        self.assertEqual(len(self.hits("walrus")), 1)
        raw = b"".join(p.read_bytes() for p in (self.tmp / "v3").iterdir() if p.is_file())
        self.assertNotIn(PLANTED.encode(), raw)

    def test_trash_spam_drafts_and_gone_messages_are_skipped(self):
        api = FakeGmail([message("t1", "x", "Body trashword", labels=("TRASH",)), message("p1", "x", "Body spamword", labels=("SPAM",)),
                         message("d1", "x", "Body draftword", labels=("DRAFT",)), message("ok", "x", "Body keepword"),
                         message("ghost", "x", "Body ghostword")])
        api.gone.add("ghost")
        self.drain(self.runner(GmailIngester(api, clock=lambda: self.now)), 4)
        self.assertEqual([d["ref"] for d in self.index.recent(9)], ["ok"])

    def test_rate_limit_and_expired_history(self):
        api = self.box(2)
        g = GmailIngester(api, clock=lambda: self.now)
        r = self.runner(g)
        self.drain(r, 3)
        api.fail = RateLimited(retry_after=900)
        out = r.tick()
        self.assertTrue(out["gmail"]["error"])
        self.assertEqual(out["gmail"]["next"] - self.now, 900)
        api.fail = None
        self.now += 900
        api.add(message("m7", "After the gap", "Body gapword words"))
        api.expired_before = api.history_id         # the stored history id is now too old
        calls = len(api.calls)
        self.drain(r, 4)
        self.assertTrue([c for c in api.calls[calls:] if c[0] == "list"])    # a new bounded backfill
        self.assertEqual(len(self.hits("gapword")), 1)
        self.assertEqual(len(self.index.recent(20)), 3)         # the re-backfill replaced, it did not duplicate
        self.assertEqual(len(self.index.read(self.hits("number0")[0]["id"])["messages"]), 1)

    def test_the_real_client_is_lazy(self):
        self.assertNotIn("httpx", sys.modules.get("jarvis.v3.ingest.gmail").__dict__)
        self.assertTrue(hasattr(gmail, "HttpGmail"))


if __name__ == "__main__":
    unittest.main()
