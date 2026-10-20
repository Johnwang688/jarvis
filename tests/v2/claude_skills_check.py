"""Claude skills in Jarvis and Codex chats (SK-1, SK-2). Free, no network.

Spec: docs/plans/2026-10-19-claude-skills-link.md §1–§5. Every root here is a
temporary directory: the real ~/.claude and ~/.codex are never read or
written (HOME, JARVIS_SKILLS and JARVIS_CLAUDE_SKILLS point at temp paths).

Run:  PYTHONPATH=<worktree> .venv/bin/python tests/v2/claude_skills_check.py

Interface these tests pin (from the plan; choices marked CHOICE):

- `config.CLAUDE_SKILLS_DIR: Path | None`. Default `~/.claude/skills`
  (`Path.home()` at import); `JARVIS_CLAUDE_SKILLS` overrides it; an empty
  value makes it `None` (root off). Read at call time by the skill code, like
  `config.SKILLS_DIR`, so tests patch the attribute.
- `jarvis.tools.skills._paths() -> dict[name, Path-to-SKILL.md]`, sorted by
  name, is the union: repo > top-level `<root>/<name>/SKILL.md` > synced
  `<root>/synced/<bucket>/<name>/SKILL.md` (first bucket in sorted order wins).
  CHOICE: tests compare `.resolve()` of the returned path, so the external
  path may be returned as written or resolved.
- External entries are skipped (the rest still load) when: the directory name
  fails `^[a-z0-9][a-z0-9-]{0,63}$`; SKILL.md does not resolve to a regular
  file named exactly `SKILL.md`; the resolved path (or the path as written)
  lies under a credential folder (`config.credential_dirs()`, expanded
  against HOME at call time: ~/.ssh, ~/.config/jarvis, ...) or a credential
  env file; the name is credential-like. CHOICE: "credential-like" is
  `jarvis.tools.secrets.credential_name(name)` (the shared env-name rule), so
  `github-token` is refused. A symlink that cannot be followed is skipped.
- An external entry that is a symlink back into the repo's `skills/` is the
  repo skill: listed once, resolving to the repo copy.
- `skill_write(name, ...)` for a name that exists only externally returns a
  string containing exactly "`<name>` lives in ~/.claude/skills; edit it
  there." (CHOICE: the literal `~/.claude/skills`, whatever the root is
  configured to) and writes nothing anywhere. Repo names, including one that
  also exists externally, still write to `SKILLS_DIR`.
- `skills.index()` is cached on a key that covers both roots.
- `commands.invocable_skills()`, `check_skill()` and `skill_body()` include
  external skills and honour `jarvis-only: true`.
- `skills_link.link(repo=, home=, unlink=, dry_run=)` keeps its signature and
  reads external skills from `config.CLAUDE_SKILLS_DIR` (CHOICE: tests set it
  to `<home>/.claude/skills`, so reading `home / ".claude/skills"` is
  equivalent). It links each external skill into `<home>/.codex/skills/<name>`
  pointing directly at the resolved skill directory; never creates anything
  for an external skill in `<home>/.claude/skills`; refuses (keeps) a foreign
  entry; `--unlink` removes only links into the repo or into a resolved
  external source, and only under `.codex` for external ones (the owner's own
  symlinks in ~/.claude/skills are the source and stay); a refused external
  entry appears as a row naming it with "refused" in the action. Repo skills
  are still linked into both consumers as today. CHOICE: an external
  `jarvis-only` skill is skipped like a repo one ("keeps today's rules").

Mutation map (each was applied to a scratch reference implementation and
made the named tests fail):
  no precedence (external overrides repo)  -> UnionAndPrecedence.test_repo_beats_top_level_beats_synced,
                                              WriteBoundary.test_repo_names_still_write_to_repo
  synced buckets in reverse order          -> test_first_synced_bucket_in_sorted_order_wins
  credential path not refused              -> Refusals.test_skill_resolving_into_ssh_refused,
                                              ..._into_jarvis_config_refused, ..._synced_bucket_...,
                                              Link.test_refused_external_entry_noted_and_not_linked
  credential-like name not refused         -> Refusals.test_credential_like_name_refused (+ Link one)
  no regular-file check                    -> test_directory_named_skill_md_refused, test_fifo_never_read
  write escapes to external root           -> WriteBoundary.test_external_only_name_*
  index key ignores external root          -> IndexKey.*
  link into ~/.claude                      -> Link.test_never_links_external_into_claude,
                                              test_second_run_is_idempotent, test_cli_...
  foreign entry replaced                   -> Link.test_never_replaces_foreign_entry,
                                              test_repo_skill_wins_codex_link
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from jarvis import config  # noqa: E402
from jarvis.tools import skills  # noqa: E402
from jarvis.v2 import commands, skills_link  # noqa: E402

LIVES = "`{}` lives in ~/.claude/skills; edit it there."


def write_skill(directory: Path, description: str, body: str = "Do the thing.",
                extra: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(f"---\nname: {directory.name}\ndescription: {description}\n{extra}---\n\n"
                    f"{body}\n", encoding="utf-8")
    return path


def snapshot(root: Path) -> dict:
    """Everything under `root` without following links: kind, target/bytes, mtime."""
    out = {}
    if not os.path.lexists(root):
        return out
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            st = p.lstat()
            if p.is_symlink():
                out[str(p.relative_to(root))] = ("link", os.readlink(p))
            elif p.is_dir():
                out[str(p.relative_to(root))] = ("dir", st.st_mtime_ns)
            elif p.is_file():
                out[str(p.relative_to(root))] = ("file", p.read_bytes(), st.st_mtime_ns)
            else:
                out[str(p.relative_to(root))] = ("other", st.st_mode)
    return out


def link_target(entry: Path) -> str:
    raw = os.readlink(entry)
    if not os.path.isabs(raw):
        raw = os.path.join(entry.parent, raw)
    return os.path.normpath(raw)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.library = self.repo / "skills"
        self.library.mkdir(parents=True)
        self.ext = self.home / ".claude" / "skills"
        self.ext.mkdir(parents=True)
        (self.home / ".codex" / "skills").mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"HOME": str(self.home)}).start()
        patch.object(config, "SKILLS_DIR", self.library).start()
        patch.object(config, "CLAUDE_SKILLS_DIR", self.ext, create=True).start()
        patch.object(skills, "_index_cache", None).start()
        patch.object(skills, "_warned_flat", set()).start()

    def paths(self) -> dict[str, Path]:
        return {name: path.resolve() for name, path in skills._paths().items()}


class UnionAndPrecedence(Base):
    """§1: the union and its precedence. [mutation: no precedence]"""

    def build(self):
        write_skill(self.library / "shared", "repo shared")
        write_skill(self.library / "alpha", "repo alpha")
        write_skill(self.ext / "shared", "top shared")
        write_skill(self.ext / "beta", "top beta")
        write_skill(self.ext / "both-ext", "top both")
        synced = self.ext / "synced"
        write_skill(synced / "b1" / "both-ext", "synced both")
        write_skill(synced / "a0" / "gamma", "a0 gamma")
        write_skill(synced / "b1" / "gamma", "b1 gamma")
        write_skill(synced / "a0" / "shared", "synced shared")
        write_skill(synced / "b1" / "delta", "b1 delta")
        (synced / "b1" / ".bucket-0123abcd").write_text("bucket marker\n")
        (synced / "manifest.json").write_text('{"skills": ["gamma"]}\n')
        (synced / "a0" / "manifest.json").write_text("{}\n")

    def test_union_contains_all_three_sources(self):
        self.build()
        paths = self.paths()
        self.assertEqual(set(paths), {"alpha", "shared", "beta", "both-ext", "gamma", "delta"})
        self.assertEqual(list(skills._paths()), sorted(skills._paths()))

    def test_repo_beats_top_level_beats_synced(self):
        self.build()
        paths = self.paths()
        self.assertEqual(paths["shared"], (self.library / "shared" / "SKILL.md").resolve())
        self.assertEqual(paths["both-ext"], (self.ext / "both-ext" / "SKILL.md").resolve())
        self.assertIn("- shared: repo shared", skills.skill_list())
        self.assertIn("- both-ext: top both", skills.skill_list())

    def test_first_synced_bucket_in_sorted_order_wins(self):
        self.build()
        self.assertEqual(self.paths()["gamma"],
                         (self.ext / "synced" / "a0" / "gamma" / "SKILL.md").resolve())
        self.assertIn("a0 gamma", skills.skill_read("gamma"))

    def test_bucket_marker_and_manifest_are_not_skills(self):
        self.build()
        names = set(self.paths())
        self.assertIn("delta", names)  # the bucket holding the marker still loads
        self.assertFalse(any(n.startswith(".bucket") or "manifest" in n or n in {"synced", "a0", "b1"}
                             for n in names), names)
        skills.index()  # neither crashes the index

    def test_symlink_back_into_repo_is_the_repo_skill(self):
        write_skill(self.library / "alpha", "repo alpha")
        (self.ext / "alpha").symlink_to(self.library / "alpha", target_is_directory=True)
        write_skill(self.ext / "beta", "top beta")
        paths = self.paths()
        self.assertEqual(paths["alpha"], (self.library / "alpha" / "SKILL.md").resolve())
        self.assertEqual(skills.skill_list().count("- alpha:"), 1)
        self.assertIn("beta", paths)

    def test_top_level_symlink_to_elsewhere_loads(self):
        elsewhere = self.root / "mine" / "linked-skill"
        write_skill(elsewhere, "owner's linked skill")
        (self.ext / "linked-skill").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.paths().get("linked-skill"), (elsewhere / "SKILL.md").resolve())

    def test_bad_directory_names_skipped(self):
        write_skill(self.ext / "good-ext", "fine")
        write_skill(self.ext / "Bad_Name", "upper and underscore")
        write_skill(self.ext / "-leading-dash", "dash first")
        write_skill(self.ext / ("a" * 65), "too long")
        write_skill(self.ext / ("b" * 64), "longest allowed")
        write_skill(self.ext / "synced" / "b0" / "Has Space", "space")
        names = set(self.paths())
        self.assertIn("good-ext", names)
        self.assertIn("b" * 64, names)
        self.assertFalse({"Bad_Name", "-leading-dash", "a" * 65, "Has Space"} & names, names)

    def test_empty_root_setting_turns_root_off(self):
        write_skill(self.ext / "good-ext", "fine")
        write_skill(self.library / "alpha", "repo alpha")
        with patch.object(config, "CLAUDE_SKILLS_DIR", None):
            self.assertEqual(set(self.paths()), {"alpha"})
        self.assertEqual(set(self.paths()), {"alpha", "good-ext"})


class Refusals(Base):
    """§1 refusals: each refused entry is skipped, the rest still load.
    [mutation: credential path not refused]"""

    def assert_only_good(self, *refused: str):
        write_skill(self.ext / "good-ext", "fine")
        names = set(self.paths())
        self.assertIn("good-ext", names)
        for name in refused:
            self.assertNotIn(name, names)
            self.assertIn("Error", skills.skill_read(name))
            self.assertNotIn(name, skills.index())
            self.assertNotIn(name, dict(commands.invocable_skills()))
        skills.skill_list()

    def test_skill_resolving_into_ssh_refused(self):
        hidden = self.home / ".ssh" / "ssh-leak"
        write_skill(hidden, "under ssh")
        (self.ext / "ssh-leak").symlink_to(hidden, target_is_directory=True)
        self.assert_only_good("ssh-leak")

    def test_skill_file_resolving_into_jarvis_config_refused(self):
        hidden = write_skill(self.home / ".config" / "jarvis" / "stash", "under jarvis config")
        (self.ext / "conf-leak").mkdir()
        (self.ext / "conf-leak" / "SKILL.md").symlink_to(hidden)
        self.assert_only_good("conf-leak")

    def test_synced_bucket_resolving_into_credential_folder_refused(self):
        hidden = self.home / ".aws" / "bucket"
        write_skill(hidden / "aws-leak", "under aws")
        (self.ext / "synced").mkdir()
        (self.ext / "synced" / "zz").symlink_to(hidden, target_is_directory=True)
        self.assert_only_good("aws-leak")

    def test_skill_md_linked_to_env_file_refused(self):
        env = self.root / "proj" / ".env"
        env.parent.mkdir()
        env.write_text("---\ndescription: OPENROUTER_API_KEY=sk-" + "x" * 30 + "\n---\n\nbody\n")
        (self.ext / "env-leak").mkdir()
        (self.ext / "env-leak" / "SKILL.md").symlink_to(env)
        self.assert_only_good("env-leak")
        self.assertNotIn("sk-" + "x" * 30, skills.index() + skills.skill_list())

    def test_skill_md_resolving_to_other_name_refused(self):
        notes = self.root / "notes" / "notes.md"
        notes.parent.mkdir()
        notes.write_text("---\ndescription: not a skill file\n---\n\nbody\n")
        (self.ext / "renamed").mkdir()
        (self.ext / "renamed" / "SKILL.md").symlink_to(notes)
        self.assert_only_good("renamed")

    def test_directory_named_skill_md_refused(self):
        (self.ext / "dir-skill" / "SKILL.md").mkdir(parents=True)
        self.assert_only_good("dir-skill")

    def test_symlink_loop_refused(self):
        (self.ext / "loop").mkdir()
        (self.ext / "loop" / "SKILL.md").symlink_to(self.ext / "loop" / "SKILL.md")
        (self.ext / "dangling").mkdir()
        (self.ext / "dangling" / "SKILL.md").symlink_to(self.root / "missing" / "SKILL.md")
        self.assert_only_good("loop", "dangling")

    def test_credential_like_name_refused(self):
        write_skill(self.ext / "github-token", "looks like a credential")
        write_skill(self.ext / "synced" / "b0" / "api-key", "synced credential name")
        self.assert_only_good("github-token", "api-key")

    def test_fifo_never_read(self):
        """A FIFO named SKILL.md would hang a reader: run in a subprocess with a
        deadline, so a missing regular-file check fails instead of hanging."""
        write_skill(self.ext / "good-ext", "fine")
        (self.ext / "fifo-skill").mkdir()
        os.mkfifo(self.ext / "fifo-skill" / "SKILL.md")
        env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(REPO),
                   JARVIS_SKILLS=str(self.library), JARVIS_CLAUDE_SKILLS=str(self.ext))
        script = ("from jarvis.tools import skills\n"
                  "print(sorted(skills._paths()))\n"
                  "skills.index(); skills.skill_list()\n"
                  "from jarvis.v2 import commands; commands.invocable_skills()\n"
                  "print('done')\n")
        try:
            out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                 text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("reading the external root hung on a FIFO named SKILL.md")
        self.assertIn("done", out.stdout, out.stderr[-2000:])
        self.assertIn("good-ext", out.stdout)
        self.assertNotIn("fifo-skill", out.stdout)


class ConfigSetting(unittest.TestCase):
    """§1: JARVIS_CLAUDE_SKILLS, read at import, in a fresh interpreter."""

    def run_config(self, **extra) -> list[str]:
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR") or None) as tmp:
            home = Path(tmp) / "home"
            write_skill(home / ".claude" / "skills" / "default-ext", "default root")
            write_skill(Path(tmp) / "other" / "other-ext", "override root")
            (Path(tmp) / "repo-skills").mkdir()
            env = {k: v for k, v in os.environ.items() if k != "JARVIS_CLAUDE_SKILLS"}
            env.update(HOME=str(home), PYTHONPATH=str(REPO),
                       JARVIS_SKILLS=str(Path(tmp) / "repo-skills"))
            env.update({k: v.replace("<tmp>", tmp) for k, v in extra.items()})
            script = ("from jarvis import config; from jarvis.tools import skills\n"
                      "print(repr(config.CLAUDE_SKILLS_DIR))\n"
                      "print(sorted(skills._paths()))\n")
            out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                 text=True, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            return out.stdout.replace(tmp, "<tmp>").splitlines()

    def test_default_is_home_claude_skills(self):
        lines = self.run_config()
        self.assertIn("<tmp>/home/.claude/skills", lines[0])
        self.assertEqual(lines[1], "['default-ext']")

    def test_override(self):
        lines = self.run_config(JARVIS_CLAUDE_SKILLS="<tmp>/other")
        self.assertIn("<tmp>/other", lines[0])
        self.assertEqual(lines[1], "['other-ext']")

    def test_empty_turns_root_off(self):
        lines = self.run_config(JARVIS_CLAUDE_SKILLS="")
        self.assertEqual(lines, ["None", "[]"])


class ReadSurfaces(Base):
    """§2–§3: skill_read, skill_list, index, `/` menu, check_skill."""

    def test_read_list_index_include_external(self):
        write_skill(self.library / "alpha", "repo alpha")
        write_skill(self.ext / "beta", "Use when testing beta", body="Beta steps.")
        write_skill(self.ext / "synced" / "b0" / "gamma", "Use when testing gamma",
                    body="Gamma steps.")
        self.assertIn("- beta: Use when testing beta", skills.skill_list())
        self.assertIn("- gamma: Use when testing gamma", skills.skill_list())
        self.assertIn("Beta steps.", skills.skill_read("beta"))
        self.assertIn("Gamma steps.", skills.skill_read("gamma"))
        index = skills.index()
        self.assertIn("- beta: Use when testing beta", index)
        self.assertIn("- gamma: Use when testing gamma", index)
        self.assertEqual(index.count("\n- "), 3)  # one line per skill

    def test_skill_read_reads_only_skill_md(self):
        write_skill(self.ext / "beta", "beta", body="See reference.md")
        (self.ext / "beta" / "reference.md").write_text("REFERENCE BODY")
        out = skills.skill_read("beta")
        self.assertIn("See reference.md", out)
        self.assertNotIn("REFERENCE BODY", out)

    def test_menu_and_check_skill_include_external_and_honour_jarvis_only(self):
        write_skill(self.ext / "beta", "beta desc", body="Beta steps.")
        write_skill(self.ext / "synced" / "b0" / "gamma", "gamma desc")
        write_skill(self.ext / "v1-only", "v1 desc", extra="jarvis-only: true\n")
        menu = dict(commands.invocable_skills())
        self.assertEqual(menu.get("beta"), "beta desc")
        self.assertEqual(menu.get("gamma"), "gamma desc")
        self.assertNotIn("v1-only", menu)
        self.assertEqual(commands.check_skill("beta"), "beta")
        self.assertEqual(commands.check_skill("gamma"), "gamma")
        self.assertIn("Beta steps.", commands.skill_body("beta"))
        with self.assertRaisesRegex(commands.SkillRefused, "only runs in the v1 loop"):
            commands.check_skill("v1-only")


class WriteBoundary(Base):
    """§2: no write path into the external root. [mutation: write escapes to external root]"""

    def test_external_only_name_refused_with_sentence_and_nothing_written(self):
        write_skill(self.ext / "beta", "beta", body="Original.")
        write_skill(self.ext / "synced" / "b0" / "gamma", "gamma", body="Original.")
        before_ext, before_repo = snapshot(self.ext), snapshot(self.library)
        for name in ("beta", "gamma"):
            out = skills.skill_write(name, "changed", "Changed body.")
            self.assertIn(LIVES.format(name), out)
        self.assertEqual(snapshot(self.ext), before_ext)
        self.assertEqual(snapshot(self.library), before_repo)
        self.assertIn("Original.", skills.skill_read("beta"))

    def test_external_only_name_via_symlink_refused(self):
        elsewhere = self.root / "mine" / "linked-skill"
        write_skill(elsewhere, "mine", body="Original.")
        (self.ext / "linked-skill").symlink_to(elsewhere, target_is_directory=True)
        before = snapshot(self.root / "mine")
        out = skills.skill_write("linked-skill", "changed", "Changed body.")
        self.assertIn(LIVES.format("linked-skill"), out)
        self.assertEqual(snapshot(self.root / "mine"), before)
        self.assertFalse((self.library / "linked-skill").exists())

    def test_repo_names_still_write_to_repo(self):
        write_skill(self.ext / "beta", "beta")  # make the external root non-empty
        write_skill(self.library / "shared", "repo shared")
        write_skill(self.ext / "shared", "top shared", body="External.")
        before_ext = snapshot(self.ext)
        self.assertIn("updated", skills.skill_write("shared", "repo v2", "Repo body."))
        self.assertIn("saved", skills.skill_write("fresh", "new one", "Fresh body."))
        self.assertEqual(snapshot(self.ext), before_ext)
        self.assertIn("Repo body.", (self.library / "shared" / "SKILL.md").read_text())
        self.assertTrue((self.library / "fresh" / "SKILL.md").is_file())
        self.assertIn("Repo body.", skills.skill_read("shared"))
        self.assertIn("beta", skills.skill_list())


class IndexKey(Base):
    """§4: the index cache key covers both roots. [mutation: index key ignores external root]"""

    def test_external_add_and_edit_show_without_restart(self):
        write_skill(self.library / "alpha", "repo alpha")
        write_skill(self.ext / "beta", "beta first")
        self.assertIn("beta first", skills.index())
        write_skill(self.ext / "gamma", "gamma added")
        self.assertIn("gamma added", skills.index())
        write_skill(self.ext / "synced" / "b0" / "delta", "delta synced")
        self.assertIn("delta synced", skills.index())
        write_skill(self.ext / "beta", "beta edited and longer")
        index = skills.index()
        self.assertIn("beta edited and longer", index)
        self.assertNotIn("beta first", index)

    def test_root_change_changes_index(self):
        write_skill(self.ext / "beta", "beta here")
        self.assertIn("beta here", skills.index())
        other = self.root / "other"
        write_skill(other / "zeta", "zeta there")
        with patch.object(config, "CLAUDE_SKILLS_DIR", other):
            index = skills.index()
            self.assertIn("zeta there", index)
            self.assertNotIn("beta here", index)
        with patch.object(config, "CLAUDE_SKILLS_DIR", None):
            self.assertNotIn("beta here", skills.index())


class Link(Base):
    """§5: `jarvis skills link` and external skills."""

    def world(self):
        write_skill(self.library / "alpha", "repo alpha")
        write_skill(self.ext / "ext-own", "owner's own")
        self.elsewhere = self.root / "mine" / "ext-via-link"
        write_skill(self.elsewhere, "linked from elsewhere")
        (self.ext / "ext-via-link").symlink_to(self.elsewhere, target_is_directory=True)
        write_skill(self.ext / "synced" / "b0" / "ext-synced", "synced")
        (self.ext / "synced" / "b0" / ".bucket-feed").write_text("x\n")
        (self.ext / "synced" / "manifest.json").write_text("{}\n")
        self.codex = self.home / ".codex" / "skills"
        self.claude = self.ext

    def link(self, **kw):
        return skills_link.link(repo=self.repo, home=self.home, **kw)

    def test_links_external_into_codex_at_resolved_dir(self):
        self.world()
        rows = self.link()
        for name, source in (("ext-own", self.ext / "ext-own"),
                             ("ext-via-link", self.elsewhere),
                             ("ext-synced", self.ext / "synced" / "b0" / "ext-synced")):
            entry = self.codex / name
            self.assertTrue(entry.is_symlink(), name)
            self.assertEqual(link_target(entry), str(source.resolve()), name)
            self.assertIn((".codex", name, "linked"), rows)
        self.assertEqual(link_target(self.codex / "alpha"), str((self.library / "alpha").resolve()))

    def test_never_links_external_into_claude(self):
        """[mutation: link into ~/.claude]"""
        self.world()
        before = set(os.listdir(self.claude))
        rows = self.link()
        self.assertEqual(set(os.listdir(self.claude)), before | {"alpha"})
        self.assertFalse(os.path.lexists(self.claude / "ext-synced"))
        for consumer, name, action in rows:
            if consumer == ".claude" and name.startswith("ext-"):
                self.assertFalse(action in {"linked", "would link"} or action.startswith("refused"),
                                 (consumer, name, action))
        self.assertTrue((self.claude / "ext-via-link").is_symlink())
        self.assertEqual(link_target(self.claude / "ext-via-link"), str(self.elsewhere))
        self.assertTrue((self.codex / "ext-own").is_symlink())

    def test_second_run_is_idempotent(self):
        self.world()
        self.link()
        before = (snapshot(self.claude), snapshot(self.codex))
        rows = self.link()
        self.assertEqual((snapshot(self.claude), snapshot(self.codex)), before)
        self.assertFalse([r for r in rows if r[2].startswith("refused")], rows)
        self.assertIn((".codex", "ext-own", "skipped: correct link"), rows)
        # ~/.claude/skills/alpha now links back into the repo: still one skill, the repo copy.
        self.assertEqual(self.paths()["alpha"], (self.library / "alpha" / "SKILL.md").resolve())

    def test_never_replaces_foreign_entry(self):
        """[mutation: foreign entry replaced]"""
        self.world()
        foreign_dir = self.codex / "ext-own"
        foreign_dir.mkdir()
        (foreign_dir / "keep").write_text("keep")
        foreign_link = self.codex / "ext-synced"
        foreign_link.symlink_to(self.root / "somewhere-else", target_is_directory=True)
        rows = self.link()
        self.assertEqual((foreign_dir / "keep").read_text(), "keep")
        self.assertFalse(foreign_dir.is_symlink())
        self.assertEqual(link_target(foreign_link), str(self.root / "somewhere-else"))
        actions = {(c, n): a for c, n, a in rows}
        self.assertTrue(actions.get((".codex", "ext-own"), "").startswith("refused"), actions)
        self.assertTrue(actions.get((".codex", "ext-synced"), "").startswith("refused"), actions)
        self.assertEqual(actions.get((".codex", "ext-via-link")), "linked")

    def test_repo_skill_wins_codex_link(self):
        write_skill(self.library / "shared", "repo shared")
        write_skill(self.ext / "shared", "top shared")
        write_skill(self.ext / "beta", "top beta")
        self.codex = self.home / ".codex" / "skills"
        self.link()
        self.assertEqual(link_target(self.codex / "beta"), str((self.ext / "beta").resolve()))
        self.assertEqual(link_target(self.codex / "shared"), str((self.library / "shared").resolve()))
        self.assertIn("top shared", (self.ext / "shared" / "SKILL.md").read_text())
        self.assertFalse((self.ext / "shared").is_symlink())

    def test_external_jarvis_only_not_linked(self):
        self.world()
        write_skill(self.ext / "v1-only", "v1", extra="jarvis-only: true\n")
        rows = self.link()
        self.assertFalse(os.path.lexists(self.codex / "v1-only"))
        self.assertIn((".codex", "v1-only", "skipped: jarvis-only"), rows)

    def test_refused_external_entry_noted_and_not_linked(self):
        self.world()
        hidden = self.home / ".ssh" / "ssh-leak"
        write_skill(hidden, "under ssh")
        (self.ext / "ssh-leak").symlink_to(hidden, target_is_directory=True)
        write_skill(self.ext / "github-token", "credential name")
        rows = self.link()
        for name in ("ssh-leak", "github-token"):
            self.assertFalse(os.path.lexists(self.codex / name), name)
            self.assertTrue(any(n == name and "refused" in a for _, n, a in rows), (name, rows))
        self.assertTrue((self.codex / "ext-own").is_symlink())

    def test_unlink_removes_only_own_links(self):
        self.world()
        self.link()
        foreign_link = self.codex / "foreign"
        foreign_link.symlink_to(self.root / "somewhere-else", target_is_directory=True)
        foreign_dir = self.codex / "own-dir"
        foreign_dir.mkdir()
        (foreign_dir / "keep").write_text("keep")
        claude_before = snapshot(self.claude)
        rows = self.link(unlink=True)
        for name in ("alpha", "ext-own", "ext-via-link", "ext-synced"):
            self.assertFalse(os.path.lexists(self.codex / name), name)
            self.assertIn((".codex", name, "unlinked"), rows)
        self.assertTrue(foreign_link.is_symlink())
        self.assertEqual((foreign_dir / "keep").read_text(), "keep")
        # The owner's own entries in ~/.claude/skills are the source: kept,
        # including the symlink to a skill outside the repo.
        claude_after = snapshot(self.claude)
        self.assertNotIn("alpha", claude_after)  # repo link removed as today
        claude_before.pop("alpha", None)
        self.assertEqual(claude_after, claude_before)
        self.assertTrue((self.claude / "ext-via-link").is_symlink())

    def test_dry_run_changes_nothing(self):
        self.world()
        before = (snapshot(self.claude), snapshot(self.codex))
        rows = self.link(dry_run=True)
        self.assertEqual((snapshot(self.claude), snapshot(self.codex)), before)
        self.assertIn((".codex", "ext-own", "would link"), rows)
        self.assertIn((".codex", "ext-synced", "would link"), rows)
        self.link()
        before = (snapshot(self.claude), snapshot(self.codex))
        rows = self.link(unlink=True, dry_run=True)
        self.assertEqual((snapshot(self.claude), snapshot(self.codex)), before)
        self.assertIn((".codex", "ext-own", "would unlink"), rows)
        self.assertIn((".codex", "ext-via-link", "would unlink"), rows)

    def test_cli_links_default_root_under_temp_home(self):
        self.world()
        env = {k: v for k, v in os.environ.items() if k != "JARVIS_CLAUDE_SKILLS"}
        env.update(HOME=str(self.home), PYTHONPATH=str(REPO))
        command = [sys.executable, "-m", "jarvis", "skills", "link"]
        out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
        self.assertIn("ext-synced", out.stdout, out.stderr[-2000:])
        self.assertEqual(link_target(self.codex / "ext-synced"),
                         str((self.ext / "synced" / "b0" / "ext-synced").resolve()))
        self.assertFalse(os.path.lexists(self.claude / "ext-synced"))
        subprocess.run(command + ["--unlink"], env=env, capture_output=True, timeout=120)
        self.assertFalse(os.path.lexists(self.codex / "ext-synced"))
        self.assertTrue((self.claude / "ext-via-link").is_symlink())


if __name__ == "__main__":
    unittest.main()
