"""Checks for the file and search tools. Free — no API, no network.

Covers the three things added 2026-08-09 and the guard they had to not break:

  read_file   line-numbered and *paged*, so a file longer than one page is
              fully reachable instead of permanently cut at 40k characters.
  edit_file   anchored replacement, and — the load-bearing one — the same
              SELF_PROTECTED refusal write_file has. A surgical edit to
              permissions.py disarms the gate exactly as thoroughly as
              overwriting it, so a new write tool that forgot that check would
              be a hole straight through the boundary, not a smaller one.
  grep_files  bounded search, both through ripgrep and through the pure-Python
              fallback, which must produce the same shapes.

Run:  .venv/bin/python tests/files_check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis import config, tools  # noqa: E402
from jarvis.tools import search as search_mod  # noqa: E402
from jarvis.tools.files import SELF_PROTECTED  # noqa: E402


def call(name: str, **kwargs) -> str:
    return tools.dispatch(name, json.dumps(kwargs)).text


# --- read_file -------------------------------------------------------------


def read_checks(tmp: Path) -> None:
    target = tmp / "long.txt"
    lines = [f"line {i}" for i in range(1, 5001)]
    target.write_text("\n".join(lines), encoding="utf-8")

    first = call("read_file", path=str(target))
    assert first.startswith("     1\tline 1\n"), repr(first[:40])
    assert "  1500\tline 1500" in first, "default page should be 1500 lines"
    assert "line 1501" not in first, "page ran past its limit"
    assert "lines 1-1500 of 5000" in first, first[-200:]
    assert "offset=1501" in first, "footer must name where to resume"

    # The CLAUDE.md case: a file far longer than one page has to be reachable
    # in full. The old reader cut at 40k characters with no way to ask for the
    # rest, so `skills/self-improve.md`'s "read CLAUDE.md first" was reading a
    # third of the file and reporting no problem.
    rebuilt: list[str] = []
    offset = 1
    while True:
        page = call("read_file", path=str(target), offset=offset, limit=800)
        body = page.split("\n\n[lines")[0]
        for row in body.split("\n"):
            rebuilt.append(row.split("\t", 1)[1])
        last = int(page.split("lines ")[1].split(" of")[0].split("-")[1])
        if last >= 5000:
            break
        offset = last + 1
    assert rebuilt == lines, f"paging lost content: {len(rebuilt)} of {len(lines)}"

    mid = call("read_file", path=str(target), offset=4990, limit=50)
    assert mid.startswith("  4990\tline 4990"), repr(mid[:40])
    assert "End of file." in mid and "offset=" not in mid

    assert "past the end" in call("read_file", path=str(target), offset=99999)
    (tmp / "empty.txt").write_text("", encoding="utf-8")
    assert call("read_file", path=str(tmp / "empty.txt")) == "[file is empty]"
    assert "does not exist" in call("read_file", path=str(tmp / "nope.txt"))
    print("ok  read_file: numbered, paged, whole file reachable by offset")


# --- edit_file -------------------------------------------------------------


def edit_checks(tmp: Path) -> None:
    target = tmp / "code.py"
    original = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    target.write_text(original, encoding="utf-8")

    out = call("edit_file", path=str(target), old_string="return 1", new_string="return 2")
    assert "has not been read" in out, out
    assert target.read_text(encoding="utf-8") == original

    call("read_file", path=str(target))

    out = call("edit_file", path=str(target), old_string="return 1", new_string="return 2")
    assert "appears 2 times" in out and "line 2" in out, out
    assert target.read_text(encoding="utf-8") == original, "ambiguous edit must not write"

    out = call(
        "edit_file",
        path=str(target),
        old_string="def a():\n    return 1",
        new_string="def a():\n    return 42",
    )
    assert "line 1" in out, out
    assert target.read_text(encoding="utf-8") == original.replace("return 1", "return 42", 1)

    out = call("edit_file", path=str(target), old_string="nowhere at all", new_string="x")
    assert "does not appear" in out, out
    out = call("edit_file", path=str(target), old_string="def b():", new_string="def b():")
    assert "identical" in out, out
    out = call("edit_file", path=str(target), old_string="", new_string="x")
    assert "empty" in out, out

    # A model quoting an excerpt back from read_file usually keeps the numbers.
    # That must land the edit, not fail to match — otherwise the display format
    # is a trap laid for the tool that depends on it.
    call("read_file", path=str(target))
    out = call(
        "edit_file",
        path=str(target),
        old_string="     4\tdef b():\n     5\t    return 1",
        new_string="def b():\n    return 99",
    )
    assert "ignored the line numbers" in out, out
    assert "return 99" in target.read_text(encoding="utf-8")

    call("read_file", path=str(target))
    out = call("edit_file", path=str(target), old_string="\ndef b():\n    return 99\n", new_string="")
    assert "def b()" not in target.read_text(encoding="utf-8"), "deletion did not apply"

    multi = tmp / "many.txt"
    multi.write_text("x\nx\nx\n", encoding="utf-8")
    call("read_file", path=str(multi))
    out = call("edit_file", path=str(multi), old_string="x", new_string="y", replace_all=True)
    assert "3 places" in out, out
    assert multi.read_text(encoding="utf-8") == "y\ny\ny\n"

    stale = tmp / "stale.txt"
    stale.write_text("before\n", encoding="utf-8")
    call("read_file", path=str(stale))
    stale.write_text("changed underneath\n", encoding="utf-8")
    os.utime(stale, (0, 0))
    out = call("edit_file", path=str(stale), old_string="changed", new_string="x")
    assert "changed on disk" in out, out
    print("ok  edit_file: unique/ambiguous/missing/stale/replace_all/numbered-paste")


def protection_checks(tmp: Path) -> None:
    """edit_file must refuse everything write_file refuses."""
    for rel in sorted(SELF_PROTECTED):
        path = config.REPO_ROOT / rel
        before = path.read_text(encoding="utf-8")
        call("read_file", path=str(path))
        # Pick a line that genuinely exists, so a refusal cannot be mistaken
        # for "the anchor simply did not match".
        anchor = next(ln for ln in before.split("\n") if ln.startswith("from ") or ln.startswith("import "))
        out = call("edit_file", path=str(path), old_string=anchor, new_string="# pwned")
        assert "safety layer" in out, f"{rel}: {out}"
        assert path.read_text(encoding="utf-8") == before, f"{rel} was modified"

    env = tmp / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-secret\n", encoding="utf-8")
    out = call("edit_file", path=str(env), old_string="sk-or-v1-secret", new_string="x")
    assert "protected" in out.lower() or "refus" in out.lower(), out
    out = call("write_file", path=str(env), content="clobbered")
    assert "protected" in out.lower() or "refus" in out.lower(), out
    assert "sk-or-v1-secret" in env.read_text(encoding="utf-8"), ".env was overwritten"
    print("ok  guard: edit_file refuses every safety-layer file and .env")


def gate_state_checks(tmp: Path) -> None:
    """SELF_PROTECTED froze the gate's code. Nothing froze the gate's *data*.

    `_self_protected()` asked `relative_to(config.REPO_ROOT)` and returned
    False on ValueError, so anything outside the checkout was unprotected by
    construction — and `config.ALLOWLIST_PATH` is deliberately outside it, at
    ~/.config/jarvis/allowlist.json. `allowlist.json` is not a credential name,
    so secrets.py did not cover it, and neither write tool is `dangerous`. So:
    read_file it (satisfying read-before-write), edit_file it to
    `[{"tool": "run_command"}]`, and because `permissions.load_allowlist()`
    re-reads the file on every check, the very next dangerous call was
    auto-approved — every surface, no restart, nobody asked, and afterwards
    indistinguishable from an entry the owner created by answering "always".

    Both write tools are in workflows.SAFE_TOOLS and SUBAGENT_TOOLS, so a
    background workflow that cannot run a dangerous tool itself could still
    unlock the owner's surfaces. `config.ALLOWLIST_PATH` is pointed at a temp
    file for this suite, and the protection is read from `config` per call, so
    this exercises the temp file and never the owner's real one.
    """
    target = config.ALLOWLIST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps([{"tool": "run_readonly", "prefix": "git"}])
    target.write_text(original, encoding="utf-8")

    seen = call("read_file", path=str(target))
    assert "run_readonly" in seen, seen  # readable: it is config, not a credential

    out = call("edit_file", path=str(target),
               old_string='"run_readonly", "prefix": "git"', new_string='"run_command"')
    assert "safety layer" in out, out
    out = call("write_file", path=str(target), content='[{"tool": "run_command"}]')
    assert "safety layer" in out, out
    assert target.read_text(encoding="utf-8") == original, "the allowlist was rewritten"

    # And the same refusal when the file does not exist yet — write_file
    # creates parents, so "not there" was the easier version of the attack.
    target.unlink()
    out = call("write_file", path=str(target), content='[{"tool": "run_command"}]')
    assert "safety layer" in out, out
    assert not target.exists(), "the allowlist was created from nothing"
    print("ok  guard: the approval gate's state file is refused by both write tools")


def model_state_checks(tmp: Path) -> None:
    """The files that decide which model answers and where Jarvis may act
    (PR #15 review, 2026-10-08). v2 already refused them; v1's write_file
    and edit_file — a background workflow's included — could rewrite
    provider_defaults.json, models.json and routing.json, which moved every
    default Claude thread from Opus to Haiku with nobody asked. Every config
    path here is a temp file (set in `main`); they stay readable."""
    from jarvis.tools import files
    from jarvis.v2 import permissions as v2perm
    from jarvis.v2 import thread_model as tm
    from jarvis.v2.model import ProviderName

    allow_dir = config.ALLOWLIST_PATH.parent
    targets = [config.MODELS_PATH, config.PROVIDER_DEFAULTS_PATH, config.DISCORD_GUILD_PATH,
               allow_dir / "routing.json", allow_dir / "models.json",
               allow_dir / "provider_defaults.json", allow_dir / "discord_guild.json"]
    assert len({t.resolve() for t in targets}) == len(targets), "each path must be distinct"
    assert files._protected_state() == v2perm.protected_paths(), \
        "v1's protected state drifted from v2's protected_paths"
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"claude": {"model": "claude-opus-5-5", "effort": None}})
        target.write_text(original, encoding="utf-8")
        seen = call("read_file", path=str(target))
        assert "claude-opus-5-5" in seen, f"{target} must stay readable: {seen}"
        out = call("edit_file", path=str(target), old_string="claude-opus-5-5",
                   new_string="claude-haiku-4-5")
        assert "safety layer" in out, f"{target}: {out}"
        out = call("write_file", path=str(target),
                   content='{"claude": {"model": "claude-haiku-4-5", "effort": null}}')
        assert "safety layer" in out, f"{target}: {out}"
        assert target.read_text(encoding="utf-8") == original, f"{target} was rewritten"
        target.unlink()
        out = call("write_file", path=str(target), content="{}")
        assert "safety layer" in out and not target.exists(), f"{target} was created: {out}"
    # The consequence the review demonstrated, now refused end to end.
    config.PROVIDER_DEFAULTS_PATH.write_text(
        json.dumps({"claude": {"model": "claude-opus-5-5", "effort": None}}), encoding="utf-8")
    call("read_file", path=str(config.PROVIDER_DEFAULTS_PATH))
    call("write_file", path=str(config.PROVIDER_DEFAULTS_PATH),
         content='{"claude": {"model": "claude-haiku-4-5", "effort": null}}')
    assert tm.default_choice(ProviderName.CLAUDE)[0] == "claude-opus-5-5"
    # An ordinary file beside them is still writable.
    out = call("write_file", path=str(allow_dir / "notes.txt"), content="fine")
    assert "Wrote" in out, out
    print("ok  guard: models.json, provider_defaults.json, routing.json and the guild file "
          "are refused by both write tools, still readable, and match v2's set")


def numbering_guard_checks(tmp: Path) -> None:
    """write_file strips read_file's numbering — and only read_file's."""
    target = tmp / "roundtrip.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    numbered = call("read_file", path=str(target))
    out = call("write_file", path=str(target), content=numbered)
    assert "stripped" in out, out
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma", target.read_text()

    # The misfire that would matter: a TSV whose first column happens to count
    # 1, 2, 3. read_file right-aligns in a 6-wide field; raw data does not, and
    # that width is the whole discriminator.
    tsv = tmp / "data.tsv"
    body = "1\tapple\n2\tbanana\n3\tcherry\n"
    tsv.write_text("placeholder\n", encoding="utf-8")
    call("read_file", path=str(tsv))
    out = call("write_file", path=str(tsv), content=body)
    assert "stripped" not in out, out
    assert tsv.read_text(encoding="utf-8") == body, "a numeric TSV column was eaten"
    print("ok  write_file: strips read_file numbering, leaves a numeric TSV alone")


# --- grep_files ------------------------------------------------------------


def grep_checks(tmp: Path) -> None:
    root = tmp / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("import os\nNEEDLE = 1\nprint(NEEDLE)\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("NEEDLE = 2\n", encoding="utf-8")
    (root / "c.txt").write_text("needle lowercase\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=NEEDLE_IN_ENV\n", encoding="utf-8")

    def check(label: str) -> None:
        out = call("grep_files", pattern="NEEDLE", path=str(root))
        assert "a.py" in out and "b.py" in out, f"{label}: {out}"
        assert ":2:NEEDLE = 1" in out or ":2:NEEDLE = 1" in out.replace("\r", ""), f"{label}: {out}"
        assert "NEEDLE_IN_ENV" not in out, f"{label}: searched a protected file"

        files = call("grep_files", pattern="NEEDLE", path=str(root), mode="files")
        assert "a.py" in files and "NEEDLE = 1" not in files, f"{label}: {files}"

        counts = call("grep_files", pattern="NEEDLE", path=str(root), mode="count")
        assert "a.py:2" in counts, f"{label}: {counts}"

        scoped = call("grep_files", pattern="NEEDLE", path=str(root), glob="b.py")
        assert "b.py" in scoped and "a.py" not in scoped, f"{label}: {scoped}"

        insensitive = call(
            "grep_files", pattern="needle", path=str(root), case_insensitive=True, mode="files"
        )
        assert "a.py" in insensitive and "c.txt" in insensitive, f"{label}: {insensitive}"

        capped = call("grep_files", pattern="NEEDLE", path=str(root), max_results=1)
        assert "showing 1 of 3 results" in capped, f"{label}: {capped}"

        ctx = call("grep_files", pattern="NEEDLE = 1", path=str(root), context_lines=1)
        assert "import os" in ctx, f"{label}: context lines missing: {ctx}"

        assert "No matches" in call("grep_files", pattern="zzzz", path=str(root)), label
        assert "not a valid regular expression" in call(
            "grep_files", pattern="(unclosed", path=str(root)
        ), label
        assert "mode must be one of" in call("grep_files", pattern="x", path=str(root), mode="wat")

    have_rg = search_mod.shutil.which("rg") is not None
    if have_rg:
        check("ripgrep")

    # The fallback is not decoration — it is what runs on a machine without
    # ripgrep, and a shape mismatch there would only ever show up in the wild.
    real_which = search_mod.shutil.which
    search_mod.shutil.which = lambda name: None
    try:
        check("forced fallback")
    finally:
        search_mod.shutil.which = real_which
    ran = "ripgrep + fallback" if have_rg else "fallback only (rg not installed)"
    print(f"ok  grep_files: modes/glob/case/cap/context, skips .env — {ran}")


def rg_args_checks() -> None:
    """The ripgrep command line, asserted directly.

    This runs whether or not ripgrep is installed, which is the point: the
    machine this was written on has no `rg`, so every "both backends" claim in
    the suite below was really the fallback twice. Checking the argument list
    covers the half that cannot otherwise be reached here.
    """
    args = search_mod._rg_args("NEEDLE", "/tmp/x", "", "content", 0, False)

    # The flag the bug was about. ripgrep respects .gitignore by default, and
    # this repo gitignores memory/*.md — so with rg installed, searching
    # Jarvis's own long-term memory silently returned nothing.
    assert "--no-ignore" in args, args

    # Skipping is explicit and shared with the fallback, not inherited from
    # whatever ignore files happen to be lying around.
    for name in search_mod.SKIP_DIRS:
        assert f"!{name}/" in args, f"{name} not excluded from the rg run: {args}"
    assert "--max-filesize" in args
    assert args[args.index("--max-filesize") + 1] == str(search_mod.MAX_FILE_BYTES)

    assert search_mod._rg_args("x", ".", "", "files", 0, False).count("--files-with-matches") == 1
    assert "--count" in search_mod._rg_args("x", ".", "", "count", 0, False)
    assert "--ignore-case" in search_mod._rg_args("x", ".", "", "content", 0, True)
    assert "--context" in search_mod._rg_args("x", ".", "", "content", 2, False)
    # The caller's own glob survives alongside the skip globs.
    assert "*.py" in search_mod._rg_args("x", ".", "*.py", "content", 0, False)
    print(f"ok  grep_files: rg invocation disables ignore files and skips {len(search_mod.SKIP_DIRS)} dirs")


def gitignore_checks(tmp: Path) -> None:
    """A gitignored file must still be searchable.

    Built as a real git repo because that is the only shape that reproduces it:
    ripgrep applies .gitignore rules only inside one. This is the case that was
    broken in the wild and green in the suite — memory/*.md is gitignored, so
    `grep_files` over Jarvis's own memory found nothing once rg was installed.
    """
    import subprocess

    root = tmp / "repo"
    (root / "memory").mkdir(parents=True)
    (root / ".gitignore").write_text("memory/*.md\nbuilt/\n", encoding="utf-8")
    (root / "memory" / "fact.md").write_text("the owner prefers KEEPSAKE\n", encoding="utf-8")
    (root / "tracked.py").write_text("KEEPSAKE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=False, capture_output=True)

    def assert_found(label: str) -> None:
        out = call("grep_files", pattern="KEEPSAKE", path=str(root), mode="files")
        assert "tracked.py" in out, f"{label}: {out}"
        assert "fact.md" in out, (
            f"{label}: a gitignored file was invisible to search — this is the "
            f"memory/*.md case: {out}"
        )

    if search_mod.shutil.which("rg"):
        assert_found("ripgrep")
    real_which = search_mod.shutil.which
    search_mod.shutil.which = lambda name: None
    try:
        assert_found("fallback")
    finally:
        search_mod.shutil.which = real_which
    print("ok  grep_files: gitignored files are still searchable (memory/*.md)")


def grep_protected_checks(tmp: Path) -> None:
    """The three ways ripgrep's output escaped the protected-file filter.

    The filter was `is_protected(ln.split(":", 1)[0])` — the first
    colon-delimited field of each output *line*. That only ever recognises one
    of rg's output shapes, and the fallback never had the bug at all because it
    filters whole *files* before reading them. Two backends, two answers, which
    is the same failure as the gitignore bug.

      * **context lines** are attributed with `-`, not `:`, so with
        `context_lines>0` every non-matching line of a credential file inside
        the window came back (`google_token.json-3-  "refresh_token": …`);
      * given a **single file** as `path`, rg prints no filename at all, so the
        field being tested was a line number — `grep_files('KEY', '.env')`
        returned the file verbatim at the default `context_lines=0`;
      * in **count** mode on a single file the output is a bare number, which
        makes a protected file a match oracle: `^OPENROUTER_API_KEY=sk-or-v1-F`
        answers 1 or 0, one character at a time, and dispatch()'s value-scrub
        cannot help because no secret value is ever in the output.

    Every case is asserted against **both** backends, and their answers must be
    identical — that agreement is the actual invariant.
    """
    root = tmp / "protected"
    root.mkdir()
    (root / ".env").write_text(
        "APP=demo\nOPENROUTER_API_KEY=sk-or-v1-FAKELEAKVALUE00000\nDEBUG=1\n", encoding="utf-8"
    )
    (root / "google_token.json").write_text(
        '{\n  "client_id": "abc",\n  "refresh_token": "1//FAKEREFRESHTOKEN"\n}\n', encoding="utf-8"
    )
    (root / "notes.txt").write_text("a token lives elsewhere\n", encoding="utf-8")

    secret_bits = ("sk-or-v1-FAKELEAKVALUE", "1//FAKEREFRESHTOKEN", "refresh_token",
                   "OPENROUTER_API_KEY")

    cases = [
        dict(pattern="token", path=str(root), context_lines=2),
        dict(pattern="token", path=str(root), context_lines=2, mode="files"),
        dict(pattern="client", path=str(root / "google_token.json"), context_lines=1),
        dict(pattern="KEY", path=str(root / ".env")),
        dict(pattern="APP", path=str(root / ".env"), mode="count"),
        dict(pattern="^OPENROUTER_API_KEY=sk-or-v1-F", path=str(root / ".env"), mode="count"),
        dict(pattern="refresh", path=str(root / "google_token.json"), mode="files"),
    ]

    real_which = search_mod.shutil.which
    have_rg = real_which("rg") is not None
    for kwargs in cases:
        with_rg = call("grep_files", **kwargs) if have_rg else None
        search_mod.shutil.which = lambda name: None
        try:
            with_py = call("grep_files", **kwargs)
        finally:
            search_mod.shutil.which = real_which

        for out, label in ((with_rg, "ripgrep"), (with_py, "fallback")):
            if out is None:
                continue
            # The "No matches for <pattern> under <path>" footer echoes only
            # the caller's own arguments back, and is the correct answer for a
            # protected target — so it is exempt from both checks. Everything
            # else is output derived from the file.
            body = "" if out.startswith("No matches") else out
            for bit in secret_bits:
                assert bit not in body, f"{label} leaked {bit!r} for {kwargs}:\n{out}"
            # A protected file must never be *attributed* a result either —
            # naming it is how mode='files' and the count oracle leak.
            assert ".env" not in body and "google_token.json" not in body, (
                f"{label} returned a result attributed to a protected file for "
                f"{kwargs}:\n{out}"
            )
        if have_rg:
            assert with_rg == with_py, (
                f"backends disagree for {kwargs}:\n--- rg ---\n{with_rg}\n--- py ---\n{with_py}"
            )

    # The count oracle specifically: a right guess and a wrong guess must be
    # indistinguishable, because neither file is searched at all.
    if have_rg:
        right = call("grep_files", pattern="^OPENROUTER_API_KEY=sk-or-v1-F",
                     path=str(root / ".env"), mode="count")
        wrong = call("grep_files", pattern="^OPENROUTER_API_KEY=sk-or-v1-Z",
                     path=str(root / ".env"), mode="count")
        # The outputs differ only in the pattern they echo back, so compare the
        # answer rather than the sentence: both must be "nothing was searched".
        assert right.startswith("No matches") and wrong.startswith("No matches"), (
            f"count mode is a match oracle: {right!r} vs {wrong!r}"
        )

    # Unprotected neighbours are still found, so this is a filter and not an
    # outage — the check that stops a do-nothing implementation from passing.
    found = call("grep_files", pattern="token", path=str(root))
    assert "notes.txt" in found, found
    ran = "both backends" if have_rg else "fallback only (rg not installed)"
    print(f"ok  grep_files: context lines, single-file paths and count mode all filtered — {ran}")


def backend_parity_checks(tmp: Path) -> None:
    """Both backends must answer identically, or the tool has two behaviours.

    Skipped loudly rather than silently when ripgrep is absent: a suite that
    quietly proves half of what its name claims is how this bug survived.
    """
    if not search_mod.shutil.which("rg"):
        print("--  grep_files: PARITY NOT VERIFIED — ripgrep is not installed here.")
        print("    Install it and re-run to check both backends agree:")
        print("      sudo apt install ripgrep")
        return

    root = tmp / "parity"
    (root / "sub" / "node_modules").mkdir(parents=True)
    (root / "sub" / "deep.py").write_text("TARGET = 1\n", encoding="utf-8")
    (root / "top.txt").write_text("TARGET here\nand TARGET again\n", encoding="utf-8")
    (root / "sub" / "node_modules" / "junk.js").write_text("TARGET\n", encoding="utf-8")
    (root / ".hidden.txt").write_text("TARGET\n", encoding="utf-8")

    real_which = search_mod.shutil.which
    for mode in ("files", "count", "content"):
        with_rg = call("grep_files", pattern="TARGET", path=str(root), mode=mode)
        search_mod.shutil.which = lambda name: None
        try:
            with_py = call("grep_files", pattern="TARGET", path=str(root), mode=mode)
        finally:
            search_mod.shutil.which = real_which
        assert sorted(with_rg.split("\n")) == sorted(with_py.split("\n")), (
            f"backends disagree in mode={mode}:\n"
            f"--- ripgrep ---\n{with_rg}\n--- python ---\n{with_py}"
        )
        assert "node_modules" not in with_rg, f"skip list not applied in mode={mode}"
        assert ".hidden.txt" not in with_rg, f"hidden file surfaced in mode={mode}"
    print("ok  grep_files: ripgrep and the Python fallback agree in all three modes")


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        # The gate's state file is now write-protected by resolved path, so it
        # has to point somewhere disposable before anything here runs — a suite
        # once wrote `apt` onto the owner's real allowlist.
        config.ALLOWLIST_PATH = tmp / "config" / "jarvis" / "allowlist.json"
        # The model and routing state is protected too (2026-10-08); each
        # path somewhere of its own, so the config path itself is what is
        # protected and not only its sibling beside the allowlist.
        config.MODELS_PATH = tmp / "models-elsewhere" / "models.json"
        config.PROVIDER_DEFAULTS_PATH = tmp / "defaults-elsewhere" / "provider_defaults.json"
        config.DISCORD_GUILD_PATH = tmp / "guild-elsewhere" / "discord_guild.json"
        config.ROUTING_PATH = tmp / "config" / "jarvis" / "routing.json"
        read_checks(tmp)
        edit_checks(tmp)
        protection_checks(tmp)
        gate_state_checks(tmp)
        model_state_checks(tmp)
        numbering_guard_checks(tmp)
        grep_checks(tmp)
        grep_protected_checks(tmp)
        rg_args_checks()
        gitignore_checks(tmp)
        backend_parity_checks(tmp)
    print("\nall file/search checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
