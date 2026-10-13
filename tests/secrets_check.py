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

from jarvis import runtime  # noqa: E402
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

    env_rule_checks(root)


# --- ENV-1: every credential env file, not only .env and .env.local --------------
#
# The owner's `.env*` rule (docs/plans/2026-10-11-env-files.md): Jarvis reads
# none of `.env` / `.env.<anything>` at any depth, except names ending
# `.example`. Each check below names a file main let through.

STAGING = "sk-staging-FAKEVALUEFORTESTSONLY-777777"
PROD = "postgres://app:FAKEPRODPASS999@db.example/app"


# The review's over-refused lines (MED-1): each allowed on main, refused at
# 153920d, allowed again — a non-dot-led glob cannot expand onto a dotfile.
ORDINARY_GLOBS = [
    "rm -f *.orig", "rm -f *.bak", "ls *.bak", "rm *.test", "ls .git/hooks/*.sample",
    "ls *test*", "ls *dev*", "ls *prod*", "ls *2*", "find . -name '*test*'",
    "find src -name '*.test*'", "find . -name '*template*'", "rg -g '*test*' foo",
    "grep -rl foo --include='*test*' .", "grep -r KEY --include=*.production .",
    "fd -g '*test*'", "find . -iname '*.bak' -delete", "tar czf out.tgz *.log",
]


def env_rule_checks(root: Path) -> None:
    from jarvis.tools import search as search_mod
    from jarvis.tools.secrets import (env_credential_in_text, env_glob_reaches, glob_reaches,
                                      is_credential)

    (root / ".env.staging").write_text(f"API_KEY={STAGING}\n", encoding="utf-8")
    (root / ".env.production").write_text(f"DATABASE_URL={PROD}\nPORT=8080\n", encoding="utf-8")
    (root / ".env.local.example").write_text("API_KEY=put-yours-here\n", encoding="utf-8")
    (root / "link-to-prod").symlink_to(".env.production")
    (root / "fake.env.example").write_text("x\n", encoding="utf-8")

    # 7. the name rule, through `is_protected` (by name) and `is_credential`
    # (symlinks too) — every listed name, case variants, the templates.
    for name in (".env.production", ".env.staging", ".env.development.local", ".env.prod.bak",
                 ".env.sample", ".env.template", ".ENV.Production", ".env.example.bak",
                 "x/.env.test", "~/app/.env.staging.local", ".env.2"):
        assert is_protected(name), name
    for name in (".env.example", ".env.local.example", ".envrc", "myapp.env", "env.production",
                 ".env-cmdrc"):
        assert not is_protected(name), name
    assert is_credential(root / "link-to-prod") and not is_protected(root / "link-to-prod")
    print("ok  names: every .env.<anything> is protected (case-insensitive), .example names are not")

    # 8. read_file, every credential env file — and the symlink — refused.
    for path in (".env.production", ".env.staging", str(root / "link-to-prod")):
        out = read_file(path)
        assert out.startswith("Error:") and "protected" in out, (path, out)
        assert PROD not in out and STAGING not in out, path
    assert "put-yours-here" in read_file(".env.local.example")
    print("ok  read_file: refuses .env.production/.env.staging and a symlink to one; "
          "reads .env.local.example")

    # 9. the shells, every smuggled spelling of the old list with the new names.
    smuggled = [
        "cat .env.production", "cat<.env.staging", "sh -c 'cat .env.production'",
        'bash -c "cat .env.staging"', "env sh -c 'cat .env.production'",
        "python -c \"print(open('.env.staging').read())\"", "F=.env.production; cat $F",
        "cat $(echo .env.staging)", "cat .ENV.PRODUCTION", "cat .env.p*", "cat .env.[sp]*", "cat .env.zz?",
        "head -1 ./x/../.env.staging", "node --env-file=.env.production -p 1",
        "docker compose --env-file .env.production up",
        "cp .env.production notes.txt", "cat .env.sample", "cat .env.example.bak",
        "cat .e\\\nnv.production",
        # ENV-1 review, LOW-1: dot-led globs that can match a credential env
        # name though none is spelled out — judged structurally, exactly.
        "cat .e?v.custom", "cat .[e]nv.custom", "cat .*.custom", "cp x .e*.custom",
        "sh -c 'cat .e?v.custom'", "grep -r KEY --include='.env.*' .", "find . -name '.env*'",
        "rg -g '.e?v.*' KEY", "ls -la ~/.*rc",       # `.*rc` can expand onto `.env.<x>rc`
    ]
    for command in smuggled:
        assert protected_in_command(command), command
        for tool in (run_readonly, run_command):
            out = tool(command) if tool is run_readonly else tool(command, reason="test")
            assert out.startswith(("Error:", "Refused:")), (command, out)
            assert PROD not in out and STAGING not in out, command
    for command in ("cat .env.example", "cat .env.local.example", "cp .env.example .env.local.example",
                    "ls *.py", "grep -r KEY --include=*.py .", "cat .envrc", "ls .git*",
                    "cat .env.*.example", "cat myapp.env.production", "echo env.production",
                    "ls -d .*.example", "ls ~/.bash*"):
        assert protected_in_command(command) is None, command
    # ENV-1 review, MED-1: ordinary globs main allowed stay allowed — a glob
    # that is not dot-led never expands onto a dotfile in bash, and an option
    # value like `*test*` is the class of `grep -r foo .`.
    for command in ORDINARY_GLOBS:
        assert protected_in_command(command) is None, command
    assert env_credential_in_text("x .env.local.example y") is None
    assert env_credential_in_text("--env-file=.env.prod") == ".env.prod"
    for pattern in (".env.zz?", ".env.p*", ".e??", ".*", ".e?v.custom", ".[e]nv.custom",
                    ".*.custom", "[.]env.x", ".?nv.x[0-9]", ".ENV.P*", ".env.example?",
                    "{a,.env}.x*", ".*rc"):
        assert glob_reaches(pattern) and env_glob_reaches(pattern), pattern
    for pattern in ("*.py", ".env.*.example", "*", "*.production", "*test*", "*.local", ".git*",
                    ".[!e]*", ".*.example", "**", "*.{bak,orig}"):
        assert not env_glob_reaches(pattern), pattern
    assert glob_reaches("google_token.*") and glob_reaches("*.json")   # bundles, by name, as on main
    assert not env_glob_reaches("*.json")
    print(f"ok  shell: {len(smuggled)} spellings of a credential env file refused in both tools; "
          f"{len(ORDINARY_GLOBS)} ordinary globs allowed")

    # 10. grep_files, both backends, every mode: .env.staging is never searched.
    real_which = search_mod.shutil.which
    for kwargs in (dict(pattern="API_KEY", path=str(root)),
                   dict(pattern="API_KEY", path=str(root), context_lines=2),
                   dict(pattern="API_KEY", path=str(root), mode="files"),
                   dict(pattern="API_KEY", path=str(root / ".env.staging"), mode="count"),
                   dict(pattern="DATABASE", path=str(root / "link-to-prod"))):
        outs = [dispatch("grep_files", json.dumps(kwargs)).text]
        search_mod.shutil.which = lambda name: None
        try:
            outs.append(dispatch("grep_files", json.dumps(kwargs)).text)
        finally:
            search_mod.shutil.which = real_which
        for out in outs:
            body = "" if out.startswith("No matches") else out
            assert STAGING not in body and PROD not in body, (kwargs, out)
            assert ".env.staging" not in body and ".env.production" not in body, (kwargs, out)
    print("ok  grep_files: .env.staging and a symlink to .env.production filtered on both backends")

    # 11. the scrub reads every credential env file in the folder chain: a
    # value copied anywhere is redacted, and a grep line attributed to one is
    # dropped — through real dispatch().
    (root / "copied.md").write_text(f"stray: {STAGING} and {PROD}\n", encoding="utf-8")
    copied = dispatch("read_file", '{"path": "copied.md"}')
    assert STAGING not in copied.text and PROD not in copied.text, copied.text
    assert copied.text.count(REDACTED) == 2, copied.text
    leak = dispatch("run_readonly", json.dumps({"command": f"grep -rn -C1 API_KEY {root}"}))
    assert STAGING not in leak.text and ".env.staging" not in leak.text, leak.text
    assert "were withheld" in leak.text, leak.text
    # A file made, or rewritten, right after the last scrub (within one
    # timestamp tick) is read at once: nothing is cached by mtime.
    qa = "qa-FAKEVALUE-made-in-the-same-tick"
    (root / ".env.qa").write_text(f"QA_KEY={qa}\n", encoding="utf-8")
    assert qa not in scrub(f"x {qa}"), "a new env file's value was missed"
    qa2 = "qa-FAKEVALUE-rewritten-same-tick"
    (root / ".env.qa").write_text(f"QA_KEY={qa2}\n", encoding="utf-8")
    assert qa2 not in scrub(f"x {qa2}"), "a rewritten env file's value was missed"
    # A FIFO named like one cannot hang the scrub (it is now read by listing).
    os.mkfifo(root / ".env.fifo")
    assert scrub("nothing secret here") == "nothing secret here"
    print("ok  scrub: values from .env.staging/.env.production redacted; a FIFO named .env.* "
          "does not hang it")

    scrub_rule_checks(root)


TOKEN40 = "Zx9Qp2Lm8Rt5Wv3Yk7Hb1Nc4Df6Gj0Ks8Lq2MxA"
STRIPE = "sk_live_FAKE51HxYzTESTONLY"
DB_PASS = "FAKEpw9zz"


def scrub_rule_checks(root: Path) -> None:
    """ENV-1 review, MED-2: from a newly covered env file only secrets are
    withheld — a secret-named key's value, or a secret-looking value — so
    settings (`NODE_ENV=production`, `API_URL=https://…`) no longer mangle
    ordinary output. Since the owner's answer (2026-10-12) `.env` and
    `.env.local` follow the same rule."""
    (root / ".env.preview").write_text(
        "NODE_ENV=production\nAPI_URL=https://api.example.com\nCOOKIE_SECURE=true\n"
        "SESSION_DRIVER=redis\nAPP_NAME=My Great Application\n"
        f"STRIPE_SECRET_KEY={STRIPE}\nDATABASE_URL=postgres://app:{DB_PASS}@db.internal/app\n"
        f"BUILD_ID={TOKEN40}\nDB_PASS=correcthorse\nexport ADMIN_PASSWORD=sunshine99\n"
        "SESSION_LIFETIME=120\n", encoding="utf-8")
    readme = ("Deploy to production with NODE_ENV=production; the API lives at "
              "https://api.example.com. COOKIE_SECURE=true, SESSION_DRIVER=redis, "
              "My Great Application.\n")
    (root / "README.md").write_text(readme, encoding="utf-8")
    out = dispatch("read_file", '{"path": "README.md"}').text
    assert "Deploy to production with NODE_ENV=production" in out, out
    assert "https://api.example.com" in out and "redis" in out and "=true" in out, out
    assert REDACTED not in out, out
    leaky = (f"key {STRIPE} url postgres://app:{DB_PASS}@db.internal/app build {TOKEN40} "
             "pass correcthorse admin sunshine99 lifetime 120")
    cleaned = scrub(leaky)
    # A secret-named key's value is withheld even when it reads as a word
    # (`DB_PASS=correcthorse`) — only the key rule catches that one.
    for value in (STRIPE, DB_PASS, TOKEN40, "correcthorse", "sunshine99"):
        assert value not in cleaned, (value, cleaned)
    assert "lifetime 120" in cleaned, cleaned
    # `.env` and `.env.local` follow the same rule now (the owner's answer,
    # 2026-10-12): a setting is left alone, a secret-named key's value and a
    # secret-looking value are still withheld.
    for name in (".env", ".env.local"):
        folder = root / ("narrow" + name)
        folder.mkdir()
        (folder / name).write_text(f"MODE=production-ish\nOPENROUTER_API_KEY={STRIPE}\n"
                                   f"BUILD_ID={TOKEN40}\nDB_PASS=correcthorse\n", encoding="utf-8")
        previous = Path.cwd()
        os.chdir(folder)
        try:
            assert "production-ish" in scrub("mode is production-ish"), (name, "a setting was withheld")
            for value in (STRIPE, TOKEN40, "correcthorse"):
                assert value not in scrub(f"value {value} here"), (name, value)
        finally:
            os.chdir(previous)
    # PR #80's review: joined *PASSWORD/*PASSWD/*CREDS names, CRED/CREDS and
    # MNEMONIC keys, with values too short or too plain for the value rule.
    joined = root / "joined"
    joined.mkdir()
    secrets_here = {"PGPASSWORD": "pgpw1234", "MYSQLPASSWORD": "mysql-pw-7",
                    "ADMINPASSWD": "adm1nzz", "DB_CREDS": "user:pw99",
                    "USERCREDS": "hunter2x", "CRED": "abcdefghijklmnopqr",
                    "MNEMONIC": "apple river stone cloud mango tiger lemon eagle piano sugar ocean candle"}
    (joined / ".env").write_text(
        "".join(f"{k}={v}\n" for k, v in secrets_here.items())
        + "KEYBOARD_LAYOUT=us-intl\nPASSWORD_MIN_LENGTH=12\nSACRED_GROVE=elm-valley\n",
        encoding="utf-8")
    previous = Path.cwd()
    os.chdir(joined)
    try:
        for key, value in secrets_here.items():
            assert value not in scrub(f"got {value} back"), (key, "withheld by the key rule")
        for setting in ("us-intl", "elm-valley"):
            assert setting in scrub(f"layout {setting}"), (setting, "a setting was withheld")
    finally:
        os.chdir(previous)
    print("ok  scrub: a .env.preview's settings left alone (README intact); its secret key, "
          "DSN password and 40-char token withheld; .env and .env.local by the same rule; joined PASSWORD/CREDS names and MNEMONIC withheld")
    delta_review_checks(root)


# --- the delta review of 4043ad1 (PR #60): pinned, every spelling -----------------
#
# Refused: each spelling main refused that 4043ad1 allowed (LOW-A, fix 4), and
# each the lead asked to close (fixes 1, 2, 3, 5, LOW-B's shell form).
DELTA_REFUSED = [
    # LOW-A — refused on main, allowed at 4043ad1.
    "find . -name '*.local' -exec cat {} +", "shopt -s dotglob; cat *.local",
    "find -name '*env*' -exec cat", "find . -name '*env*' -exec cat {} +",
    "find . -name '*.local'", "rg -g '*.local' KEY",
    # Fix 1: a line that turns dotglob on judges every glob word exactly.
    "shopt -s dotglob; cat *custom*", "bash -O dotglob -c 'cat *custom*'",
    "GLOBIGNORE=x; cat *.custom", "zsh -c 'cat *custom*(D)'", "setopt GLOB_DOTS; cat *.custom",
    "cat *(D)", "ls *(D)",
    # Fix 2: a name pattern that names env and can match one, its files read.
    "find . -name '*.env.production' -exec cat {} +",
    "find . -name '*.env.prod' -print0 | xargs -0 cat", "grep -r KEY --include='*.env.prod' .",
    "rg --hidden -g '*.env*' API_KEY", "fd -H -g '*.env*' -x cat",
    "grep -r API_KEY --include='*.env*' .", "find . -name '*.env*' -exec cat {} +",
    "find . -name '*.env.local' -exec cat {} +", "grep -r KEY --include=*.env* .",
    "zip -r o.zip . -i '*.env.prod'",
    # Fix 3: a glued short option's value.
    "rg --hidden -g'.e?v*' KEY", "rg --hidden -g.e?v* KEY .", "rg -g.e?v* KEY .",
    # Fix 4 (option A): patterns a program expands itself.
    "git grep --no-index KEY -- '.e?v*'", "git grep --no-index -h KEY -- '.e?v*'",
    "tar --wildcards -xOf b '.e?v*'", "tar --wildcards -xOf b.tar '.e?v*'", "zip -i '.e?v*'",
    "zip -r o.zip . -i '.e?v*'", "git grep KEY -- '.e?v.custom'", "git ls-files -- '.e?v.c*'",
    "tar --wildcards -xOf b.tar '.e?v.custom'", "unzip -p a.zip '.e?v.custom'",
    "7z e a.7z '.e?v.custom'",
    # Fix 5: strings another program runs as a command line.
    "node -e \"require('child_process').execSync('cat .e?v.custom')\"",
    "node --eval \"require('child_process').execSync('cat .e?v.custom')\"",
    "perl -e 'system(\"cat .e?v.custom\")'", "perl -e 'print `cat .e?v.custom`'",
    "env -S 'cat .e?v.custom'", "env -S'cat .e?v.custom'", "watch 'cat .e?v.custom'",
    "watch -n1 'cat .e?v.custom'", "ssh h 'cat .e?v.custom'", "bash <<< 'cat .e?v.custom'",
    "python3 <<< \"import os; os.system('cat .e?v.custom')\"",
    "sh -c \"find . -name '.e?v*' -exec cat {} +\"",
    # The delta review's LOW-1 list, refused at 4043ad1 and kept.
    "cat .e?v.custom", "cat .[e]nv.custom", "cat .*.custom", "cat .e*.custom", "cat [.]env.custom",
    "cat .E?V.CUSTOM", "find . -name '.e?v*' -exec cat {} +", "find . -iname '.E*' -exec cat {} +",
    "rg -g '.e?v*' --hidden KEY", "rg --glob='.e?v*' --hidden KEY",
    "grep -r KEY --include='.e?v*' .", "grep -r KEY --include .e?v* .", "fd -H -g '.e?v*' -x cat",
    "sh -c 'cat .e?v.custom'", "bash -lc \"cat .e*.custom\"", "eval 'cat .e?v.custom'",
    "find . -type f -exec sh -c 'cat .e?v.custom' \\;",
    "python3 -c \"import os; os.system('cat .e?v.custom')\"", "cat .e{n,x}v.custom",
    "printf '%s\\n' .e?v* | xargs cat", "find . -path './.e?v*' -exec cat {} +",
    "su u -c 'cat .e?v.custom'", "git -c alias.x='!cat .e?v.custom' x",
    # Main's floor on a word bash does not expand.
    "echo '*.local'", "grep '.*env.*' log",
]
# Allowed: the review's ordinary list, and lines each fix must not catch.
DELTA_ALLOWED = [
    "rm -f *.orig", "find . -name '*.orig' -delete", "rm -f *.bak", "ls *.bak",
    "ls .git/hooks/*.sample", "rm *.test", "ls *test*", "find . -name '*test*'",
    "find src -name '*.test*'", "find . -name '*template*'", "ls *dev*", "ls *prod*", "ls *2*",
    "rg -g '*test*' foo", "grep -rl foo --include='*test*' .", "ls *.local", "cat *.production",
    "pkill -f '.*daemon2'", "grep -E '.*error.*' log.txt", "jq '.items[] | .name' x.json",
    "find . -name '*.env.ts'", "git log --grep='.*fix'", "ls .git*", "ls -d .bash*",
    "cat .env.example", "cat .env.*.example",
    # Not dot-led: bash's `?` never matches a leading dot (the review's 21st spelling).
    "cat ?env.custom",
    # Fix 2 needs `env` in the pattern (or main's names): these read no more
    # than `grep -r foo .`, the stated limit.
    "find . -name '*.py' -exec grep -l foo {} +", "grep -r foo --include='*.py' .",
    "rg -g '*.py' foo", "rg --hidden -g '*.py' foo", "find . -name '*.ts' | xargs wc -l",
    "find . -name '*.custom' -exec cat {} +", "grep -r KEY --include='*.custom' .",
    "rg --hidden -g '*.custom' KEY", "fd -H -g '*.custom' -x cat",
    "find . -name '*.env.ts' -delete",
    # Fix 5 reads string literals that hold a command line, not a program's regexes.
    "perl -ne 'print if /(.*)foo/' x.txt", "perl -pe 's/.*: //' x.txt",
    "node -e \"console.log(/.*foo/.test('a b'))\"", "grep -e '.* failed' log",
    "watch 'ls -la'", "ssh h 'ls *.log'", "ssh h 'tail -f /var/log/syslog'",
    "env -S 'python3 -u'", "bash <<< 'echo hi'",
    # Fix 4 judges only dot-led (or main's) patterns.
    "git grep -n '.*TODO' -- src", "git ls-files -- '*.py'", "tar --wildcards -xf b.tar '*.py'",
    "zip -r o.zip . -x '*.pyc'", "unzip -l a.zip",
    # Fix 1 needs dotglob; `$(date)` is not a zsh qualifier; a glued `-g` without a glob.
    "echo $(date) && ls *.py", "gcc -g3 -o x x.c", "npm install -g pnpm",
    "git commit -m 'fix: date $(date)'", "timeout 5 git grep foo",
]


def delta_review_checks(root: Path) -> None:
    from jarvis.tools.secrets import secret_values

    for command in DELTA_REFUSED:
        assert protected_in_command(command), f"want refused: {command}"
    for command in DELTA_ALLOWED:
        assert protected_in_command(command) is None, f"want allowed: {command}"
    print(f"ok  delta review: {len(DELTA_REFUSED)} spellings refused, "
          f"{len(DELTA_ALLOWED)} ordinary lines allowed")

    # LOW-C: the selective scrub, both directions.
    withheld = {
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T0FAKE/B0FAKE/fakeFAKEfake1234",
        "DISCORD_WEBHOOK": "https://discord.com/api/webhooks/123456/fakeTOKENfake",
        "SOME_CALLBACK": "https://api.example.com/v1/cb/a1B2c3D4e5F6g7H8i9J0",
        "APIKEY": "fk12ab34cd", "SECRETKEY": "fk98zy76xw",
        "PASSPHRASE": "correct horse battery staple", "SMTP_PW": "fakepw7788",
        "ADMIN_PIN": "48291357", "OTP_SEED": "JBSWYDPEHPKPXPQR", "ACCESSTOKEN": "abcd1234",
        "SEEDPHRASE": "apple banana cherry", "REDIS_PASSWORD_URL": "redis://:fakepw@cache:6379",
    }
    passing = {
        "AUTH_TOKEN_TTL": "3600", "SESSION_TIMEOUT": "1800", "PASSWORD_LENGTH": "12",
        "TOKEN_URL": "https://auth.example.com/oauth/token", "KEY_NAME": "prod-key",
        "SECRET_PATH": "/etc/app/secrets", "PIN_ENABLED": "false", "EMAIL_FROM": "noreply@example.com",
        "DOCS_URL": "https://github.com/acme/app/tree/main/docs", "KEYBOARD_LAYOUT": "dvorak",
        "MAPPING_FILE": "config/map.json", "TOKENIZER_MODEL": "bert", "LOG_LEVEL": "debug",
    }
    delta = root / "delta"
    delta.mkdir()
    (delta / ".env.production").write_text(
        "".join(f"{k}={v}\n" for k, v in {**withheld, **passing}.items()), encoding="utf-8")
    previous = Path.cwd()
    os.chdir(delta)
    try:
        values = set(secret_values())
        for key, value in withheld.items():
            assert value in values, f"want withheld: {key}"
        for key, value in passing.items():
            assert value not in values, f"want passing: {key}"
        assert "expires in 3600 seconds" in scrub("expires in 3600 seconds")
    finally:
        os.chdir(previous)
    print(f"ok  scrub (delta review): {len(withheld)} webhook URLs, joined keys, PINs and "
          f"seeds withheld; {len(passing)} settings (AUTH_TOKEN_TTL=3600 …) left alone")


if __name__ == "__main__":
    # dispatch() refuses when no toolset is bound (PR #34's follow-up), so
    # this suite says what it calls directly — and is held to it.
    with runtime.tools_bound({"read_file", "run_readonly", "run_command", "grep_files"}):
        sys.exit(main())
