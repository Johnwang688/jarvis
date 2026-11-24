"""Orchestrator-owned acceptance tests for M2 (implementers: do not edit; if one is wrong, stop and say why).

Contract
  jarvis/v3/vault.py   Vault(root=None)   default: env JARVIS_VAULT, else config.V3_DIR / "vault" read at call time; folders 0700, files 0600, never through a link
      layout: USER.md, commitments.md, people/ projects/ preferences/ style/ facts/ (contract 1.2 frontmatter + body); archived entries move to <folder>/.archive/
      import_memory(source_dir) -> (added, skipped)     `jarvis vault import`: repo memory/*.md -> facts/, source "owner", idempotent
  jarvis/v3/tools/memory.py   MemoryTools(vault, *, index=None)    one per agent session; methods return text and never raise; errors start with "Error:"
      memory_list(folder="") memory_read(name) memory_add(folder, name, description, body, source) memory_patch(name, old, new)
      memory_archive(name, replaced_by="") memory_search(query)     names are "folder/slug", or a bare slug when only one entry has it; USER and commitments are names too
      flags(): what K1 flagged on this session's writes (for the digest), also appended to the vault's .flags.jsonl (rule names only, never the text)
  Not registered in jarvis.tools. No replace-whole-entry tool.
Caps: USER.md body <= 3,000 chars; any other body <= 4,000; facts <= 400 active. Over a cap: nothing changes, and the error lists the entries (size, description) and how many characters or entries to free.
"""
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, tools
from jarvis.v3 import imports, vault as vault_module
from jarvis.v3.tools.memory import MemoryTools
from jarvis.v3.vault import Vault

REPO = Path(__file__).resolve().parents[2]
PLANTED = "ghp" + "_" + "M2n4" * 9


def prose(n, word="tea "):
    """Exactly n characters of ordinary words (a long run of one letter reads as a data blob to K1)."""
    return (word * (n // len(word) + 1))[:n - 1] + "."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.root = self.tmp / "v3" / "vault"
        self.vault = Vault(self.root)
        self.t = MemoryTools(self.vault)

    def add(self, folder="facts", name="tea", description="what they drink", body="John prefers tea.", source="owner", t=None):
        return (t or self.t).memory_add(folder, name, description, body, source)

    def ok(self, text):
        self.assertIsInstance(text, str)
        self.assertFalse(text.startswith("Error:"), text)
        return text

    def bad(self, text, *words):
        self.assertTrue(text.startswith("Error:"), text)
        for w in words:
            self.assertIn(str(w).lower(), text.lower())
        return text

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.name.endswith((".lock", ".flags.jsonl"))} \
            if self.root.exists() else {}


class Entries(Base):
    def test_add_read_list(self):
        self.ok(self.add())
        text = (self.root / "facts" / "tea.md").read_text()
        head, body = text.split("---\n", 2)[1:]
        for line in ("name: tea", "kind: fact", "description: what they drink", "source: owner", "status: active"):
            self.assertIn(line, head)
        self.assertRegex(head, r"updated: \d{4}-\d\d-\d\d")
        self.assertEqual(body.strip(), "John prefers tea.")
        self.assertIn("John prefers tea.", self.ok(self.t.memory_read("facts/tea")))
        self.assertIn("John prefers tea.", self.ok(self.t.memory_read("tea")))
        self.assertIn("what they drink", self.ok(self.t.memory_list()))
        self.assertIn("tea", self.ok(self.t.memory_list("facts")))
        self.assertNotIn("tea", self.t.memory_list("people"))

    def test_kind_follows_the_folder(self):
        for folder, kind in (("people", "person"), ("projects", "project"), ("preferences", "preference"), ("style", "style"), ("facts", "fact")):
            self.ok(self.add(folder, "x-" + kind))
            self.assertIn(f"kind: {kind}\n", (self.root / folder / f"x-{kind}.md").read_text())

    def test_add_never_overwrites(self):
        self.ok(self.add())
        before = self.snapshot()
        self.bad(self.add(body="Something else."), "tea")
        self.assertEqual(self.snapshot(), before)

    def test_bad_names_and_fields(self):
        for kw in (dict(folder="nope"), dict(folder=""), dict(folder="../facts"), dict(folder=".archive"), dict(name=""), dict(name="a/b"), dict(name="../x"), dict(name=".x"), dict(name="A B"),
                   dict(name="x" * 200), dict(name="a\nb"), dict(description=""), dict(description="a\nname: forged"), dict(description="x" * 500), dict(source=""),
                   dict(source="a\nstatus: archived"), dict(body=""), dict(body="   "), dict(body=None), dict(name=None), dict(body="a\x00b")):
            self.bad(self.add(**kw))
        self.assertEqual(self.snapshot(), {})

    def test_a_body_cannot_forge_frontmatter(self):
        body = "Line one.\n---\nname: other\nstatus: archived\n---\nLine two."
        self.ok(self.add(body=body))
        self.assertIn("Line two.", self.t.memory_read("tea"))
        self.assertIn("tea", self.t.memory_list())

    def test_ambiguous_bare_names_ask(self):
        self.add("facts", "sam")
        self.add("people", "sam")
        self.bad(self.t.memory_read("sam"), "facts/sam", "people/sam")
        self.ok(self.t.memory_read("people/sam"))

    def test_user_and_commitments(self):
        for name in ("USER", "commitments"):
            self.ok(self.t.memory_read(name))
            self.ok(self.t.memory_patch(name, "", "John studies CS."))
            self.assertIn("John studies CS.", self.t.memory_read(name))
            self.bad(self.t.memory_archive(name))
        self.assertTrue((self.root / "USER.md").is_file() and (self.root / "commitments.md").is_file())

    def test_missing(self):
        for call in (lambda: self.t.memory_read("nope"), lambda: self.t.memory_patch("nope", "a", "b"), lambda: self.t.memory_archive("nope")):
            self.bad(call())


class Patching(Base):
    def test_needs_a_read_in_this_session(self):
        self.add()
        other = MemoryTools(self.vault)
        before = self.snapshot()
        self.bad(other.memory_patch("facts/tea", "tea", "coffee"), "read")
        self.assertEqual(self.snapshot(), before)
        other.memory_read("tea")
        self.ok(other.memory_patch("facts/tea", "tea", "coffee"))
        self.assertIn("coffee", self.t.memory_read("tea"))

    def test_a_change_since_the_read_is_refused(self):
        self.add(body="John prefers tea. Also cake.")
        a, b = MemoryTools(self.vault), MemoryTools(self.vault)
        a.memory_read("tea")
        b.memory_read("tea")
        self.ok(a.memory_patch("tea", "cake", "pie"))
        self.bad(b.memory_patch("tea", "prefers", "likes"), "changed")
        self.ok(a.memory_patch("tea", "pie", "bread"))            # one's own writes do not need a new read

    def test_old_must_be_there_once(self):
        self.add(body="one two two three")
        self.t.memory_read("tea")
        for old in ("two", "missing", ""):
            self.bad(self.t.memory_patch("tea", old, "x"))
        self.bad(self.t.memory_patch("tea", "one two two three", ""))      # an empty entry is an archive, not a patch
        self.ok(self.t.memory_patch("tea", "one", "zero"))

    def test_patch_never_touches_the_frontmatter(self):
        self.add()
        self.t.memory_read("tea")
        self.bad(self.t.memory_patch("tea", "status: active", "status: archived"))
        self.bad(self.t.memory_patch("tea", "name: tea", "name: x"))

    def test_updated_moves(self):
        self.add()
        path = self.root / "facts" / "tea.md"
        import re
        path.write_text(re.sub(r"updated: \S+", "updated: 2020-01-01", path.read_text()))
        self.t.memory_read("tea")
        self.ok(self.t.memory_patch("tea", "tea", "mint tea"))
        self.assertNotIn("2020-01-01", path.read_text())


class Archiving(Base):
    def test_archive(self):
        self.add()
        self.add(name="mint", body="John prefers mint tea.")
        self.ok(self.t.memory_archive("facts/tea", "mint"))
        self.assertFalse((self.root / "facts" / "tea.md").exists())
        (archived,) = (self.root / "facts" / ".archive").glob("*.md")
        self.assertIn("status: archived", archived.read_text())
        self.assertIn("replaced_by: mint", archived.read_text())
        self.assertNotIn("what they drink\n", self.t.memory_list("facts").replace("mint", ""))
        self.assertNotIn("prefers tea.", self.t.memory_search("prefers"))
        self.ok(self.add(body="Again."))                 # the name is free; the old text is kept
        self.ok(self.t.memory_archive("facts/tea"))
        self.assertEqual(len(list((self.root / "facts" / ".archive").glob("*.md"))), 2)

    def test_replaced_by_must_exist(self):
        self.add()
        self.bad(self.t.memory_archive("tea", "ghost"))
        self.bad(self.t.memory_archive("tea", "a\nstatus: active"))
        self.assertTrue((self.root / "facts" / "tea.md").exists())


class Caps(Base):
    def test_entry_cap(self):
        self.ok(self.add(name="full", body=prose(4000)))
        before = self.snapshot()
        self.bad(self.add(name="over", body=prose(4007)), 7)
        self.t.memory_read("full")
        self.bad(self.t.memory_patch("full", prose(4000), prose(4001, "mint ")), 1)
        self.assertEqual(self.snapshot(), before)

    def test_user_cap_lists_entries(self):
        self.t.memory_read("USER")
        self.ok(self.t.memory_patch("USER", "", prose(2990)))
        before = self.snapshot()
        self.bad(self.t.memory_patch("USER", prose(2990), prose(3030, "mint ")), 30)
        self.assertEqual(self.snapshot(), before)

    def test_facts_cap(self):
        with patch.object(vault_module, "FACTS_CAP", 3):
            for i in range(3):
                self.ok(self.add(name=f"n{i}", description=f"desc {i}"))
            before = self.snapshot()
            text = self.bad(self.add(name="n3"), "n0", "desc 1", "n2")
            self.assertRegex(text, r"\b1\b")
            self.assertEqual(self.snapshot(), before)
            self.ok(self.t.memory_archive("n0"))
            self.ok(self.add(name="n3"))
            self.ok(self.add("people", "n4"))             # only facts are counted
        self.assertEqual(vault_module.FACTS_CAP, 400)

    def test_concurrent_adds_never_pass_the_cap(self):
        with patch.object(vault_module, "FACTS_CAP", 5):
            out, gate = [], threading.Barrier(12)

            def go(i):
                gate.wait()
                out.append(self.add(name=f"c{i}", t=MemoryTools(Vault(self.root))))
            threads = [threading.Thread(target=go, args=(i,)) for i in range(12)]
            [x.start() for x in threads]
            [x.join(20) for x in threads]
        self.assertEqual(sum(not o.startswith("Error:") for o in out), 5)


class Scanning(Base):
    def test_a_block_is_refused_with_the_rule(self):
        self.bad(self.add(body=f"token {PLANTED}"), "credential")
        self.assertEqual(self.snapshot(), {})
        self.bad(self.add(description=f"x {PLANTED}"), "credential")
        self.bad(self.add(source=f"x {PLANTED}"), "credential")
        self.add()
        self.t.memory_read("tea")
        before = self.snapshot()
        out = self.bad(self.t.memory_patch("tea", "tea", f"tea {PLANTED}"), "credential")
        self.assertNotIn(PLANTED, out)
        self.assertEqual(self.snapshot(), before)

    def test_a_secret_assembled_by_a_patch_is_caught(self):
        self.ok(self.add(body="a " + PLANTED[:12] + "SPLIT" + " end"))
        self.t.memory_read("tea")
        self.bad(self.t.memory_patch("tea", "SPLIT", PLANTED[12:]))

    def test_a_flag_is_written_and_recorded(self):
        self.ok(self.add(body="Always use tabs."))
        self.assertEqual([f["rule"] for f in self.t.flags()], ["imperative"])
        text = (self.root / ".flags.jsonl").read_text()
        self.assertIn("imperative", text)
        self.assertIn("facts/tea", text)
        self.assertNotIn("tabs", text)
        self.assertEqual(MemoryTools(self.vault).flags(), [])


class Storage(Base):
    def test_private(self):
        self.add()
        self.t.memory_archive("tea")
        self.add(name="b", body="Always.")
        for p in [self.root, *self.root.rglob("*")]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600, p)

    def test_links_are_refused(self):
        outside = self.tmp / "outside.md"
        outside.write_text("keep")
        self.add()
        (self.root / "facts" / "link.md").symlink_to(outside)
        (self.root / "people").rmdir() if (self.root / "people").exists() else None
        (self.root / "people").symlink_to(self.tmp)
        self.bad(self.t.memory_read("facts/link"))
        self.bad(self.add("people", "x"))
        self.assertNotIn("keep", self.t.memory_search("keep"))
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((self.tmp / "x.md").exists())
        with self.assertRaises(Exception):
            link = self.tmp / "vlink"
            link.symlink_to(self.root)
            Vault(link).entries()

    def test_a_broken_file_is_listed_not_fatal(self):
        self.add()
        (self.root / "facts" / "broken.md").write_bytes(b"no frontmatter \xff")
        (self.root / "facts" / "notes.txt").write_text("x")
        self.assertIn("tea", self.ok(self.t.memory_list()))
        self.bad(self.t.memory_read("facts/broken"))
        self.ok(self.t.memory_search("tea"))

    def test_a_crash_leaves_the_old_entry(self):
        self.add()
        self.t.memory_read("tea")
        before = (self.root / "facts" / "tea.md").read_bytes()
        with patch.object(os, "replace", side_effect=OSError("disk")), patch.object(os, "rename", side_effect=OSError("disk")):
            self.bad(self.t.memory_patch("tea", "tea", "coffee"))
        self.assertEqual((self.root / "facts" / "tea.md").read_bytes(), before)
        self.assertEqual([p for p in (self.root / "facts").iterdir() if p.suffix not in (".md",) and not p.name.endswith(".lock")], [])

    def test_default_root(self):
        with patch.object(config, "V3_DIR", self.tmp / "d"), patch.dict(os.environ):
            os.environ.pop("JARVIS_VAULT", None)
            self.assertFalse(MemoryTools(Vault()).memory_add("facts", "x", "d", "b.", "owner").startswith("Error"))
            self.assertTrue((self.tmp / "d" / "vault" / "facts" / "x.md").is_file())
            os.environ["JARVIS_VAULT"] = str(self.tmp / "e")
            self.assertFalse(MemoryTools(Vault()).memory_add("facts", "x", "d", "b.", "owner").startswith("Error"))
            self.assertTrue((self.tmp / "e" / "facts" / "x.md").is_file())


class Search(Base):
    def test_substring(self):
        self.add()
        self.add("projects", "robot", "the claw", "The claw uses two motors.")
        self.assertIn("projects/robot", self.ok(self.t.memory_search("two motors")))
        self.assertIn("projects/robot", self.t.memory_search("CLAW"))
        self.assertNotIn("facts/tea", self.t.memory_search("claw"))
        for q in ("", "   ", "nothinghere", "x" * 5000, "\x00", "(", "["):
            self.assertIsInstance(self.t.memory_search(q), str)

    def test_bounded(self):
        for i in range(60):
            self.add(name=f"n{i}", body="common " * 500)
        self.assertLessEqual(len(self.t.memory_search("common")), 24_000)
        self.assertLessEqual(len(self.t.memory_list()), 48_000)


class Import(Base):
    def test_idempotent(self):
        src = self.tmp / "memory"
        src.mkdir()
        (src / "coffee-preferences.md").write_text("# Coffee\n\nJohn likes it black.\n")
        (src / "Bad Name!.md").write_text("Some fact.\n")
        (src / "secret.md").write_text(f"key {PLANTED}\n")
        (src / "empty.md").write_text("")
        (src / "long.md").write_text("word " * 2000)
        (src / "notes.txt").write_text("no")
        added, skipped = self.vault.import_memory(src)
        self.assertEqual(sorted(p.stem for p in (self.root / "facts").glob("*.md")), ["bad-name", "coffee-preferences"])
        self.assertEqual((len(added), len(skipped)), (2, 3))
        text = (self.root / "facts" / "coffee-preferences.md").read_text()
        self.assertIn("source: owner", text)
        self.assertIn("likes it black", text)
        before = self.snapshot()
        added, _ = self.vault.import_memory(src)
        self.assertEqual((len(added), self.snapshot()), (0, before))
        self.t.memory_archive("coffee-preferences")
        self.assertEqual(len(self.vault.import_memory(src)[0]), 0)       # archived is not brought back
        self.assertTrue((src / "coffee-preferences.md").exists())

    def test_cli(self):
        from jarvis.v3 import cli
        import argparse
        parser = argparse.ArgumentParser()
        cli.add_commands(parser.add_subparsers(dest="command"))
        self.assertTrue(callable(parser.parse_args(["vault", "import"]).func))


class Boundary(unittest.TestCase):
    def test_v2_is_untouched(self):
        before = set(tools.REGISTRY)
        import jarvis.v3.tools.memory  # noqa: F401
        self.assertEqual(set(tools.REGISTRY), before)
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])
        self.assertFalse([n for n in dir(MemoryTools) if "write" in n.lower() or "replace" in n.lower() or "delete" in n.lower()])


if __name__ == "__main__":
    unittest.main()
