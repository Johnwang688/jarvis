"""Orchestrator-owned acceptance tests for M3 (implementers: do not edit; if one is wrong, stop and say why).

Contract
  jarvis/v3/index.py
    Index(path=None)                         default config.V3_DIR / "index.sqlite"; 0600 in a 0700 folder, never through a link; close(); context manager
      tables docs, chunks, cursors, FTS5 chunks_fts (porter unicode61) and a trigram FTS5 table
      add(source, ref, *, title, ts, messages, meta=None) -> int   appends messages [(role, text, ts|None)] to the doc (source, ref); returns chunks added
      discover(query, limit=5), scroll(doc, around, limit), read(doc, page=1), recent(limit) -> dataclasses/dicts the tool formats
    Every text passes jarvis.v2.redact.for_model before insert; a text that cannot be redacted is not stored.
  jarvis/v3/ingest/transcripts.py
    ingest(index, *, claude=None, codex=None, jarvis=None, budget_s=None, stop=None) -> Stats(files, lines, chunks, skipped, done: bool)
      roots default to ~/.claude/projects, ~/.codex/sessions, config.SESSIONS_DIR; incremental by byte offset in `cursors`
  jarvis/v3/tools/session_search.py
    session_search(query="", mode="discover", doc="", around=0, limit=5, *, index=None) -> str       errors are text, never raised
      discover: best hit per session, up to `limit` sessions: the hit +-5 messages and the session's first 3 and last 3
      scroll: messages around `around` (a seq) in `doc`; read: the whole doc, paged by `around` (page number); recent: newest sessions
    Redacted again on output, fenced as untrusted text, capped at 24,000 characters. Not registered in jarvis.tools (v2 never offers it).
Indexed: user and assistant text, tool names, and tool results cut to 2,000 characters. Not indexed: thinking, system/developer text, sidechains, images.
"""
import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, tools
from jarvis.v2 import redact
from jarvis.v3 import imports
from jarvis.v3.index import Index
from jarvis.v3.ingest import transcripts
from jarvis.v3.tools.session_search import session_search

REPO = Path(__file__).resolve().parents[2]
PLANTED = "ghp" + "_" + "Q7w8" * 9


def c_user(text, **kw):
    return {"type": "user", "sessionId": "s", "timestamp": "2026-11-20T10:00:00Z", "message": {"role": "user", "content": text}, **kw}


def c_asst(*blocks, **kw):
    return {"type": "assistant", "sessionId": "s", "timestamp": "2026-11-20T10:01:00Z", "message": {"role": "assistant", "content": list(blocks)}, **kw}


T = lambda t: {"type": "text", "text": t}


def x_item(role, text, kind=None):
    kind = kind or ("output_text" if role == "assistant" else "input_text")
    return {"type": "response_item", "timestamp": "2026-11-21T10:00:00Z",
            "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.roots = {k: self.tmp / k for k in ("claude", "codex", "jarvis")}
        for r in self.roots.values():
            r.mkdir()
        self.path = self.tmp / "v3" / "index.sqlite"
        self.index = Index(self.path)
        self.addCleanup(self.index.close)

    def claude(self, rows, name="proj/a.jsonl", mode="w"):
        path = self.roots["claude"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode) as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        return path

    def codex(self, rows, name="2026/11/21/rollout-1.jsonl"):
        path = self.roots["codex"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def ingest(self, **kw):
        return transcripts.ingest(self.index, **{**self.roots, **kw})

    def search(self, query="", **kw):
        return session_search(query, index=self.index, **kw)

    def dump(self):
        db = sqlite3.connect(self.path)
        try:
            return "\n".join(str(r) for t in ("docs", "chunks", "cursors") for r in db.execute(f"select * from {t}"))
        finally:
            db.close()


class Ingest(Base):
    def test_three_sources(self):
        self.claude([c_user("the quokka build broke"), c_asst(T("fixed the quokka linker"))])
        self.codex([x_item("user", "wombat tests please"), x_item("assistant", "wombat suite is green")])
        s = self.roots["jarvis"] / "20261122-1"
        s.mkdir()
        (s / "log.jsonl").write_text(json.dumps({"role": "user", "text": "remember the numbat"}) + "\n")
        (s / "meta.json").write_text(json.dumps({"id": "20261122-1", "title": "Numbat talk"}))
        stats = self.ingest()
        self.assertEqual((stats.files, stats.done), (3, True))
        for word in ("quokka", "wombat", "numbat"):
            self.assertIn(word, self.search(word), word)

    def test_what_is_not_indexed(self):
        self.claude([c_asst({"type": "thinking", "thinking": "zzthought"}), c_user("zzside", isSidechain=True), {"type": "system", "content": "zzsystem"},
                     {"type": "attachment", "attachment": {"text": "zzattach"}}, {"type": "api-request-blob", "message": {"role": "system", "content": [T("zzblob")]}},
                     {"type": "queue-operation", "content": "zzqueue"}, {"type": "session-owner", "accountUuid": "zzaccount"},
                     c_user([{"type": "image", "source": {"data": "zzimage"}}]), c_user("kept words"),
                     c_asst({"type": "tool_use", "name": "Bash", "input": {"command": "ls zzinput"}}),
                     c_user([{"type": "tool_result", "content": "head " + "y" * 5000 + " zztail"}])])
        self.codex([x_item("developer", "zzdeveloper"), x_item("system", "zzsys"), {"type": "session_meta", "payload": {"base_instructions": "zzbase", "creator_user_id": "zzcreator"}},
                    {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "zzenc", "summary": [{"text": "zzsummary"}]}},
                    {"type": "world_state", "payload": {"state": "zzstate"}}, {"type": "event_msg", "payload": {"type": "task_started", "x": "zzevent"}},
                    x_item("user", "codex kept")])
        self.ingest()
        text = self.dump()
        for word in ("zzthought", "zzside", "zzsystem", "zzattach", "zzblob", "zzqueue", "zzaccount", "zzimage", "zztail", "zzdeveloper", "zzsys", "zzbase", "zzcreator",
                     "zzenc", "zzsummary", "zzstate", "zzevent"):
            self.assertNotIn(word, text, word)
        for word in ("kept words", "codex kept", "Bash", "head yyy"):
            self.assertIn(word, text, word)

    def test_incremental(self):
        self.claude([c_user("first line alpha")])
        self.assertEqual(self.ingest().lines, 1)
        self.assertEqual((self.ingest().lines, self.ingest().chunks), (0, 0))
        self.claude([c_user("second line beta")], mode="a")
        s = self.ingest()
        self.assertEqual((s.lines, s.chunks), (1, 1))
        self.assertEqual(self.dump().count("first line alpha"), 1)

    def test_a_partial_last_line_waits(self):
        path = self.claude([c_user("whole line")])
        half = json.dumps(c_user("torn line gamma"))
        with path.open("a") as f:
            f.write(half[:40])
        self.ingest()
        self.assertNotIn("gamma", self.dump())
        with path.open("a") as f:
            f.write(half[40:] + "\n")
        self.ingest()
        self.assertEqual(self.dump().count("torn line gamma"), 1)

    def test_truncated_or_replaced_files_are_reread_once(self):
        path = self.claude([c_user("old words one"), c_user("old words two")])
        self.ingest()
        path.write_text(json.dumps(c_user("new start")) + "\n")
        self.ingest()
        self.ingest()
        text = self.dump()
        self.assertEqual((text.count("new start"), text.count("old words")), (1, 0))
        other = path.with_suffix(".new")
        other.write_text("".join(json.dumps(c_user(f"replaced {i} " + "pad " * 30)) + "\n" for i in range(5)))
        os.replace(other, path)
        self.ingest()
        text = self.dump()
        self.assertEqual((text.count("replaced 0"), text.count("new start")), (1, 0))

    def test_a_deleted_transcript_keeps_its_history(self):
        path = self.claude([c_user("keepme")])
        self.ingest()
        path.unlink()
        self.ingest()
        self.assertIn("keepme", self.search("keepme"))

    def test_bad_lines_are_skipped_not_fatal(self):
        path = self.claude([c_user("good one")])
        with path.open("ab") as f:
            f.write(b"not json\n[]\nnull\n{\"type\": \"user\"}\n{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":{\"x\":1}}}\n\xff\xfe\n" + b"[" * 100_000 + b"\n")
            f.write(json.dumps(c_user("good two")).encode() + b"\n")
        s = self.ingest()
        self.assertGreaterEqual(s.skipped, 3)
        self.assertIn("good two", self.dump())

    def test_huge_lines_are_bounded(self):
        path = self.claude([c_user("small")])
        with path.open("a") as f:
            f.write(json.dumps(c_user("big " * 4_000_000)) + "\n")
        self.claude([c_user("after the big one")], mode="a")
        start = time.monotonic()
        self.ingest()
        self.assertLess(time.monotonic() - start, 20)
        self.assertIn("after the big one", self.dump())

    def test_a_budget_stops_and_the_next_run_goes_on(self):
        for i in range(30):
            self.claude([c_user(f"file {i} word{j}") for j in range(200)], name=f"proj/f{i}.jsonl")
        runs = 0
        while not self.ingest(budget_s=0.05).done:
            runs += 1
            self.assertLess(runs, 2000)
        self.assertGreater(runs, 0)
        self.assertEqual(sum("file %d word0'" % i in self.dump() or f"file {i} word0" in self.dump() for i in range(30)), 30)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("select count(*) from chunks").fetchone()[0], 6000)
        db.close()

    def test_stop(self):
        self.claude([c_user("x")])
        stop = threading.Event()
        stop.set()
        self.assertFalse(self.ingest(stop=stop).done)

    def test_links_and_other_files_are_not_followed(self):
        secret = self.tmp / "outside.jsonl"
        secret.write_text(json.dumps(c_user("zzoutside")) + "\n")
        (self.roots["claude"] / "link.jsonl").symlink_to(secret)
        (self.roots["claude"] / "dir").symlink_to(self.tmp)
        os.mkfifo(self.roots["claude"] / "pipe.jsonl")
        for name in (".env.jsonl", "notes.txt", ".credentials.json", "proj/.env.production.jsonl"):
            p = self.roots["claude"] / name
            p.parent.mkdir(exist_ok=True)
            p.write_text(json.dumps(c_user("zzname")) + "\n")
        self.claude([c_user("fine")])
        self.ingest()
        text = self.dump()
        self.assertNotIn("zzoutside", text)
        self.assertNotIn("zzname", text)
        self.assertIn("fine", text)

    def test_missing_roots(self):
        s = transcripts.ingest(self.index, claude=self.tmp / "no", codex=self.tmp / "no2", jarvis=self.tmp / "no3")
        self.assertEqual((s.files, s.done), (0, True))

    def test_t3_and_other_databases_are_never_opened(self):
        (self.roots["claude"] / "statev2.sqlite").write_bytes(b"SQLite format 3\0")
        real = sqlite3.connect
        seen = []
        with patch.object(sqlite3, "connect", side_effect=lambda p, *a, **k: seen.append(str(p)) or real(p, *a, **k)):
            self.ingest()
        self.assertFalse([p for p in seen if "statev2" in p])


class Redaction(Base):
    def test_stored_and_returned_redacted(self):
        self.claude([c_user(f"token {PLANTED} quoll"), c_asst({"type": "tool_use", "name": "Bash", "input": {"command": f"echo {PLANTED}"}}),
                     c_user([{"type": "tool_result", "content": [T(f"out {PLANTED}")]}])])
        self.codex([x_item("user", f"codex {PLANTED}")])
        self.ingest()
        self.assertNotIn(PLANTED, self.dump())
        for p in self.path.parent.iterdir():
            self.assertNotIn(PLANTED.encode(), p.read_bytes(), p.name)
        self.assertNotIn(PLANTED, self.search("quoll"))

    def test_output_is_redacted_even_if_the_store_is_not(self):
        with patch.object(redact, "for_model", side_effect=lambda t: t):
            self.index.add("claude", "x", title="t", ts=1.0, messages=[("user", f"bilby {PLANTED}", None)])
        self.assertIn("bilby", self.search("bilby"))
        self.assertNotIn(PLANTED, self.search("bilby"))

    def test_a_token_split_by_the_chunker_is_still_gone(self):
        for pad in range(3960, 4040, 7):
            self.index.add("claude", f"p{pad}", title="t", ts=1.0, messages=[("user", "a" * (pad - 20) + " " + PLANTED + " end", None)])
        self.assertNotIn(PLANTED[:20], self.dump())

    def test_unredactable_text_is_not_stored(self):
        with patch.object(redact, "for_model", side_effect=RuntimeError("x")):
            self.index.add("claude", "y", title="t", ts=1.0, messages=[("user", "zzraw", None)])
        self.assertNotIn("zzraw", self.dump())


class Search(Base):
    def setUp(self):
        super().setUp()
        rows = [c_user("opening question about kiwis")] + [c_asst(T(f"reply number {i}")) for i in range(40)] + [c_user("the needle platypus is here")] + \
               [c_asst(T(f"later {i}")) for i in range(40)] + [c_asst(T("final words"))]
        self.claude(rows)
        self.claude([c_user("def parse_frobnicate(x):"), c_asst(T("platypus again in another session"))], name="proj/b.jsonl")
        self.ingest()

    def test_discover(self):
        out = self.search("needle platypus", limit=1)
        for part in ("needle platypus", "opening question", "final words", "reply number 39", "later 0"):
            self.assertIn(part, out)
        self.assertNotIn("reply number 20", out)
        self.assertIn("another session", self.search("platypus", limit=5))

    def test_substring_and_code(self):
        self.assertIn("parse_frobnicate", self.search("frobnic"))
        self.assertIn("parse_frobnicate", self.search("frobnicate(x"))

    def test_scroll_read_recent(self):
        out = self.search("needle platypus", limit=1)
        import re
        doc = re.search(r"doc[=: ]+(\S+)", out).group(1).strip("`,;)]")
        seq = int(re.search(r"(?:#|seq[=: ]+)(\d+)[^\n]*needle|\[(\d+)\][^\n]*needle", out).group(1) or 0) if re.search(r"#(\d+)[^\n]*needle", out) else 41
        self.assertIn("needle platypus", self.search(mode="scroll", doc=doc, around=seq))
        page1 = self.search(mode="read", doc=doc, around=1)
        self.assertIn("opening question", page1)
        self.assertIn("proj", self.search(mode="recent"))

    def test_odd_input_is_text_not_an_exception(self):
        for kw in (dict(query=""), dict(query="   "), dict(query='"unclosed'), dict(query="a AND OR NOT ("), dict(query="*"), dict(query="x" * 10_000), dict(query="col:val NEAR/3"),
                   dict(query="\x00"), dict(query="日本"), dict(query="q", mode="nope"), dict(mode="scroll", doc="missing"), dict(mode="read", doc="", around=-4),
                   dict(query="q", limit=0), dict(query="q", limit=10 ** 9), dict(mode="scroll", doc="'; drop table docs;--")):
            out = self.search(**kw)
            self.assertIsInstance(out, str)
            self.assertTrue(out)
            self.assertLessEqual(len(out), 24_000)
        self.assertIn("platypus", self.search("platypus"))

    def test_output_is_fenced_and_bounded(self):
        for i in range(8):
            self.claude([c_user("bigword " + "lorem " * 5000) for _ in range(12)], name=f"proj/big{i}.jsonl")
        self.ingest()
        out = self.search("bigword", limit=20)
        self.assertLessEqual(len(out), 24_000)
        from jarvis import untrusted
        self.assertTrue(untrusted.has_fence(out))
        self.assertIsNone(untrusted.unclosed_fence(out))

    def test_no_index_yet(self):
        with patch.object(config, "V3_DIR", self.tmp / "empty"):
            self.assertIsInstance(session_search("x"), str)

    def test_reading_never_blocks_on_a_writer(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute("begin immediate")
        start = time.monotonic()
        self.assertIn("platypus", self.search("platypus"))
        self.assertLess(time.monotonic() - start, 2)
        db.execute("rollback")
        db.close()


class Store(Base):
    def test_private(self):
        self.claude([c_user("x")])
        self.ingest()
        for p in self.path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600, p.name)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_a_linked_index_is_refused(self):
        target = self.tmp / "other.sqlite"
        target.write_bytes(b"")
        link = self.tmp / "v3" / "link.sqlite"
        link.symlink_to(target)
        with self.assertRaises(Exception):
            Index(link)
        self.assertEqual(target.read_bytes(), b"")

    def test_two_indexers_do_not_duplicate(self):
        self.claude([c_user(f"dup{i}") for i in range(300)])
        other = Index(self.path)
        threads = [threading.Thread(target=transcripts.ingest, args=(i,), kwargs=self.roots) for i in (self.index, other)]
        [t.start() for t in threads]
        [t.join(60) for t in threads]
        other.close()
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("select count(*) from chunks").fetchone()[0], 300)
        db.close()

    def test_a_corrupt_index_is_set_aside_and_rebuilt(self):
        self.claude([c_user("survivor")])
        self.index.close()
        self.path.write_bytes(b"not a database" * 500)
        self.index = Index(self.path)
        self.ingest()
        self.assertIn("survivor", self.search("survivor"))
        self.assertTrue([p for p in self.path.parent.iterdir() if "corrupt" in p.name])

    def test_v2_never_sees_the_tool(self):
        before = set(tools.REGISTRY)
        import jarvis.v3.tools.session_search  # noqa: F401
        self.assertEqual(set(tools.REGISTRY), before)
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])


class Speed(Base):
    def test_search_is_fast(self):
        for d in range(40):
            self.index.add("claude", f"d{d}", title=f"doc {d}", ts=float(d),
                           messages=[("user", f"common words {d} {i} " + "lorem ipsum dolor " * 20 + (" rareword" if (d, i) == (7, 70) else ""), None) for i in range(150)])
        for query in ("rareword", "common words", "lorem ipsum", "nothinghere", "ommon wor"):
            start = time.perf_counter()
            self.search(query)
            self.assertLess(time.perf_counter() - start, 0.3, query)


if __name__ == "__main__":
    unittest.main()
