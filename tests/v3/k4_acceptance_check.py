"""Orchestrator-owned acceptance tests for K4 (implementers: do not edit; if one is wrong, stop and say why).

v3's own data becomes protected state for Jarvis's v1 and v2 agents (T3 sessions are outside, H10).
  config: V3_POLICY_FILE (V3_DIR/policy.json, env JARVIS_POLICY_FILE), V3_SKILLS_DIR (V3_DIR/skills, env JARVIS_LEARNED_SKILLS), V3_SKILL_LEDGER_DIR (V3_DIR/skill-ledger),
          V3_REVIEW_QUEUE_DIR (V3_DIR/review-queue), V3_BUDGET_LEDGER (V3_DIR/budget.jsonl)
  Files: policy.json, budget.jsonl, ~/.claude.json, ~/.claude/settings.json, ~/.claude/settings.local.json, ~/.codex/config.toml, ~/.codex/hooks.json
  Trees (every file at or below): skills, skill-ledger, review-queue
  Write tools and v2 grants refuse all of them. Reads stay as they are. Nothing else under V3_DIR changes (vault, index, feed ...).
  Shell judging is not touched: `protected_state.py` and `rules.py` are byte-identical to main.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import config, permissions as P1, rules
from jarvis.tools import files
from jarvis.v2 import permissions as P2
from jarvis.v3 import imports

REPO = Path(__file__).resolve().parents[2]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home)}))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        v3 = self.home / "share" / "v3"
        self.v3 = v3
        for name, value in (("V3_DIR", v3), ("V3_POLICY_FILE", v3 / "policy.json"), ("V3_SKILLS_DIR", v3 / "skills"), ("V3_SKILL_LEDGER_DIR", v3 / "skill-ledger"),
                            ("V3_REVIEW_QUEUE_DIR", v3 / "review-queue"), ("V3_BUDGET_LEDGER", v3 / "budget.jsonl")):
            self.enterContext(patch.object(config, name, value))
        self.files = [v3 / "policy.json", v3 / "budget.jsonl", self.home / ".claude.json", self.home / ".claude" / "settings.json", self.home / ".claude" / "settings.local.json",
                      self.home / ".codex" / "config.toml", self.home / ".codex" / "hooks.json"]
        self.trees = [v3 / "skills", v3 / "skill-ledger", v3 / "review-queue"]
        self.inside = [self.trees[0] / "release-steps" / "SKILL.md", self.trees[0] / ".pending" / "x" / "SKILL.md", self.trees[1] / "ledger.jsonl", self.trees[1] / "blobs" / "ab", self.trees[2] / "1.json"]
        self.free = [v3 / "vault" / "facts" / "tea.md", v3 / "index.sqlite", v3 / "feed" / "a.json", v3 / "policy.json.txt", v3 / "skills2" / "x", self.home / ".claude" / "notes.md", self.home / ".codex" / "AGENTS.md",
                     self.home / "project" / "policy.json", self.home / "project" / "skills" / "x" / "SKILL.md", self.home / "project" / ".claude" / "settings.json"]
        for p in [*self.files, *self.inside, *self.free]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")

    def writes(self, p):
        return (f"echo x > {p}", f"echo x >> {p}", f"cp /tmp/a {p}", f"mv /tmp/a {p}", f"tee {p}", f"sed -i s/a/b/ {p}", f"rm {p}", f"truncate -s0 {p}", f"ln -sf /tmp/a {p}", f"install -m600 /tmp/a {p}",
                f"dd if=/tmp/a of={p}", f"rsync /tmp/a {p}", f"bash -c 'echo x > {p}'", f"cd {p.parent} && echo x > {p.name}", f"cat a | tee -a {p}", f"chmod 777 {p}")

    def reads(self, p):
        return (f"cat {p}", f"head -n 3 {p}", f"grep -n x {p}", f"wc -l {p}", f"ls -la {p.parent}", f"stat {p}")


class WriteTools(Base):
    def test_refused(self):
        for p in [*self.files, *self.inside, *self.trees, *[t / "new" / "x" for t in self.trees]]:
            self.assertTrue(files._self_protected(p), p)
            self.assertTrue(P2.denied_file(str(p)), p)

    def test_links_do_not_launder(self):
        link = self.home / "innocent"
        link.symlink_to(self.v3)
        self.assertTrue(files._self_protected(link / "policy.json"))
        self.assertTrue(files._self_protected(link / "skills" / "a" / "SKILL.md"))
        self.assertTrue(P2.denied_file(str(link / "skill-ledger" / "ledger.jsonl")))

    def test_everything_else_is_as_before(self):
        for p in self.free:
            self.assertFalse(files._self_protected(p), p)

    def test_unresolvable_refuses(self):
        for error in (OSError("no"), RuntimeError("loop")):
            with patch.object(files, "_v3_paths", side_effect=error):
                self.assertTrue(files._self_protected(self.free[0]))
                self.assertTrue(P2.denied_file(str(self.free[0])))
                self.assertTrue(P2.denied_grant(str(self.v3)))

    def test_grants(self):
        for p in (*self.trees, self.v3, self.home / ".codex", self.home / ".claude"):
            self.assertTrue(P2.denied_grant(str(p)), p)


class Shell(Base):

    def test_reads_stay(self):
        for p in (self.v3 / "policy.json", *self.inside, self.v3 / "budget.jsonl"):
            for line in self.reads(p):
                self.assertEqual(P1.static_verdict(line).decision, rules.ALLOW, line)

    def test_program_reads_stay(self):
        """A program that only reads a record is judged as it is for any other file."""
        other = self.home / "project" / "data.json"
        for p in (self.v3 / "policy.json", self.v3 / "budget.jsonl", self.inside[0], self.home / ".claude.json"):
            for line in ("python3 -c \"print(open('{}').read())\"", "jq . {}", "node -e \"console.log(require('fs').readFileSync('{}','utf8'))\"", "diff {} /tmp/a", "cp {} /tmp/copy"):
                got, want = P1.static_verdict(line.format(p)), P1.static_verdict(line.format(other))
                self.assertNotEqual(got.decision, rules.DENY, line.format(p))
                self.assertIn(got.decision, (want.decision, rules.ASK), line.format(p))



class Codex(Base):
    def test_no_writable_root_reaches_it(self):
        from jarvis.v2.providers import codex_config
        for p in (self.v3, *self.trees, self.trees[0] / "a"):
            p.mkdir(parents=True, exist_ok=True)
            self.assertFalse(codex_config._safe_root(p), p)


class Config(unittest.TestCase):
    def test_paths(self):
        for name, tail in (("V3_POLICY_FILE", "policy.json"), ("V3_SKILLS_DIR", "skills"), ("V3_SKILL_LEDGER_DIR", "skill-ledger"), ("V3_REVIEW_QUEUE_DIR", "review-queue"), ("V3_BUDGET_LEDGER", "budget.jsonl")):
            self.assertEqual(getattr(config, name), config.V3_DIR / tail)
        self.assertEqual(imports.violations(REPO / "jarvis/v3"), [])


if __name__ == "__main__":
    unittest.main()
