"""Synthetic checks for .env / .env.local protection. Free — no API calls.

Verifies, against a throwaway directory holding a fake key:
  1. name matching — .env and .env.local are protected, .env.example is not
  2. read_file refuses them, and still reads the neighbouring .env.example
  3. run_readonly / run_command refuse a command that names one, including a
     dotted glob, while leaving ordinary commands alone
  4. the dispatch() backstop — replays the real leak (a recursive grep that
     never names .env) and checks the key does not survive into the result
  5. a value copied into some other file is redacted there too
  6. short values (DEBUG=1) are left alone, so unrelated output survives

Run:  .venv/bin/python tests/secrets_check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.tools import dispatch  # noqa: E402
from jarvis.tools.files import read_file  # noqa: E402
from jarvis.tools.secrets import (  # noqa: E402
    REDACTED,
    is_protected,
    protected_in_command,
    scrub,
)
from jarvis.tools.shell import run_command, run_readonly  # noqa: E402

SECRET = "sk-or-v1-FAKEKEYFORTESTSONLY-0123456789abcdef"
LOCAL_SECRET = "postgres://user:FAKEPASSWORD123@localhost/db"


def build_fixture(root: Path) -> None:
    (root / ".env").write_text(
        f"# not a real key\nOPENROUTER_API_KEY={SECRET}\nDEBUG=1\n", encoding="utf-8"
    )
    (root / ".env.local").write_text(f"DATABASE_URL={LOCAL_SECRET}\n", encoding="utf-8")
    (root / ".env.example").write_text("OPENROUTER_API_KEY=sk-or-v1-...\n", encoding="utf-8")
    (root / "notes.md").write_text(
        f"debug mode is DEBUG=1 here\nold copy of the key: {SECRET}\n", encoding="utf-8"
    )
    (root / "app").mkdir()
    (root / "app" / "settings.py").write_text(
        'KEY = os.environ["OPENROUTER_API_KEY"]\n', encoding="utf-8"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        build_fixture(root)
        previous = Path.cwd()
        os.chdir(root)
        try:
            run_checks(root)
        finally:
            os.chdir(previous)

    print("all secret protection checks passed")
    return 0


def run_checks(root: Path) -> None:
    # 1. name matching
    for path in (".env", ".env.local", "./x/.env", "~/.env.local", str(root / ".env")):
        assert is_protected(path), path
    for path in (".env.example", "env", "environment.md", ".envrc"):
        assert not is_protected(path), path
    print("ok  names: .env/.env.local protected, .env.example and .envrc are not")

    # 2. read_file
    for path in (".env", ".env.local", str(root / ".env")):
        out = read_file(path)
        assert out.startswith("Error:") and "protected" in out, out
        assert SECRET not in out and LOCAL_SECRET not in out, path
    assert "sk-or-v1-..." in read_file(".env.example")
    print("ok  read_file: refuses both, still reads .env.example")

    # 3. shell pre-checks
    blocked = [
        "cat .env",
        "cat ./.env",
        "head -5 .env.local",
        "cat .env*",
        "cat .e??",
        f"cat {root / '.env'}",
        "cat '.env'",
    ]
    for command in blocked:
        assert protected_in_command(command), command
        out = run_readonly(command)
        assert out.startswith("Error:") and "protected" in out, (command, out)
        assert SECRET not in out, command
        out = run_command(command, reason="test")
        assert out.startswith("Error:") and "protected" in out, (command, out)
    for command in ("cat .env.example", "grep -RIn KEY app", "ls -la", "cat notes.md"):
        assert protected_in_command(command) is None, command
    print(f"ok  shell: {len(blocked)} protected forms refused in both tools, globs included")

    # 4. the backstop — the exact shape of the leak this was written for
    leak = "grep -RIn OPENROUTER_API_KEY ."
    assert protected_in_command(leak) is None, "the pre-check is not what catches this"
    raw = run_readonly(leak)
    assert SECRET in raw, "fixture is wrong — the raw grep should expose the key"
    result = dispatch("run_readonly", f'{{"command": "{leak}"}}')
    assert SECRET not in result.text, result.text
    assert ".env:" not in result.text, result.text
    assert "were withheld" in result.text, result.text
    assert "app/settings.py" in result.text, "unrelated grep hits should survive"
    print("ok  dispatch: recursive grep leaks the key, the scrubber removes it")

    # 4b. layer 2, the shapes where the filename is not a token of its own.
    # `protected_in_command` treated every shlex token as a path and took its
    # basename, so anything that glued the name to an operator or buried it in
    # an interpreter payload was invisible. `env sh -c 'cat .env'` is the one
    # that mattered: `env` is on run_readonly's allowlist, `-c`'s payload is a
    # single token, and run_readonly is ungated — so a live credential reached
    # the transcript with no human in the loop.
    smuggled = [
        "cat<.env",
        "sh -c 'cat .env'",
        'bash -c "cat .env"',
        "env sh -c 'cat .env'",
        "python -c \"print(open('.env').read())\"",
        "F=.env; cat $F",
        "cat $(echo .env)",
        "cat .env.local",
    ]
    for command in smuggled:
        assert protected_in_command(command), command
        out = run_command(command, reason="test")
        assert out.startswith("Error:") and "protected" in out, (command, out)
        assert SECRET not in out and LOCAL_SECRET not in out, command
    # ...without swallowing the names that are meant to stay readable.
    for command in ("cat .env.example", "cat .envrc", "cat app/settings.py",
                    "grep -RIn KEY app", "cat myapp.env"):
        assert protected_in_command(command) is None, command
    print(f"ok  shell: {len(smuggled)} smuggled spellings of a protected name refused")

    # 4c. layer 3, the separator it did not know about. grep attributes a
    # *context* line as `path-N-text`, not `path:N:text`, so `grep -C2` walked
    # a credential file straight past the scrub — and silently, because the
    # "[N line(s) … withheld]" counter still printed.
    for sep in (("-", "-"), (":", ":"), ("\t", "\t"), ("\x00", ":")):
        attributed = "\n".join(
            f"{root / '.env'}{sep[0]}{n}{sep[1]}{body}"
            for n, body in ((1, "# not a real key"), (2, f"OPENROUTER_API_KEY={SECRET}"),
                            (3, "DEBUG=1"))
        )
        cleaned = scrub(attributed)
        assert SECRET not in cleaned, (sep, cleaned)
        assert "OPENROUTER_API_KEY" not in cleaned, (sep, cleaned)
        assert "DEBUG=1" not in cleaned, (sep, cleaned)  # short value, so only the drop saves it
        assert "were withheld" in cleaned, (sep, cleaned)
    # A separator that is present but does not attribute anything protected
    # must not start eating output.
    ordinary = "app/settings.py:1:KEY = os.environ[...]\nnotes-2026-08.md-4-plain text"
    assert scrub(ordinary) == ordinary, scrub(ordinary)
    print("ok  scrub: context-line, tab and NUL attributions are dropped like colon ones")

    # 4d. the same gap end-to-end, through real dispatch(). The pattern matches
    # line 1 of .env, so lines 2 and 3 arrive as *context* — the shape that
    # survived. The key name alone leaks structure, which is why the rule is
    # that the whole attributed line goes.
    ctx_leak = f"grep -rn -C2 'not a real key' {root}"
    result = dispatch("run_readonly", json.dumps({"command": ctx_leak}))
    raw_ctx = run_readonly(ctx_leak)
    assert f"{root}/.env-2-" in raw_ctx, f"fixture is wrong — no context line: {raw_ctx}"
    assert SECRET not in result.text, result.text
    for shape in (f"{root}/.env-", f"{root}/.env:"):
        assert shape not in result.text, result.text
    assert "app/settings.py" in run_readonly(f"grep -rn KEY {root}"), "fixture sanity"
    print("ok  dispatch: a context-line grep no longer walks a .env past the scrub")

    # 5. a copy of the value in another file
    copied = dispatch("read_file", '{"path": "notes.md"}')
    assert SECRET not in copied.text, copied.text
    assert REDACTED in copied.text, copied.text
    print("ok  scrub: a value copied into another file is redacted there too")

    # 6. short values are left alone
    assert "debug mode is DEBUG=1 here" in copied.text, copied.text
    assert scrub("port 1 and DEBUG=1") == "port 1 and DEBUG=1"
    print("ok  scrub: short values (DEBUG=1) do not mangle unrelated output")


if __name__ == "__main__":
    sys.exit(main())
