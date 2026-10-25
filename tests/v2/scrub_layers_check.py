"""Free checks: one shared credential scrub, a display and a model-input side
(scrub hardening, 2026-10-25).

`jarvis/v2/redact.py` holds the shapes (`SHAPES` plus the private-key block,
`Bearer`/`Basic`, URL password and AWS secret rules), matched on a normalized
view (fullwidth folded; zero-width, bidi, soft-hyphen, control and combining
characters dropped; a token split across one whitespace character joined).
Two scrubs are built on it:

- `redact.for_model` (alias `scrub_text`): known values and shapes, nothing
  else, for what an agent reads (chat relays, chat git output, sub-agent
  words, compaction reasons, diff review's input);
- `redact.for_display`: `for_model` plus `labelled_values`, for what the owner
  reads (the desk's Diff tab, the transcript's `chat_reads.scrub`, the
  Discord mirror and reporter, approval cards).

Cases:
  s1  every credential shape in the fixture table is withheld, marked
      `[withheld: <kind>]`, on every display and model-input path
  s2  split (NBSP, tab, newline, U+2028, U+0085), fullwidth, combining-mark,
      zero-width, soft-hyphen and bidi variants are withheld on every path,
      and the text outside the withheld span is unchanged
  s3  the keep set (PR #308's 33 lines and its code lines, plus hashes,
      UUIDs, base64 image data, `Bearer " + token`, templates, prose about
      prefixes, non-ASCII text) passes the model-input scrub byte for byte,
      and the display scrub too; the review's labelled probe lines pass the
      model-input scrub unchanged (bar the AWS secret, now a shape) and are
      withheld on display
  s4  the desk's Diff tab route (`GET /tasks/<id>/diff`, `/diff/file`)
      withholds the shapes and labelled values; diff review's model input
      keeps the labelled value and ordinary code
  s5  `approvals.card_args`: a private key in a `Write`'s `content` or an
      `Edit`'s `new_string` is withheld whole, on the card and in
      `log.jsonl`'s args (`daemon._log_fields`); a card stays faithful (a
      command keeps every character the shell reads; a key at a command's
      place stays, as `inline_credentials_check` pins)
  s6  mutations, each caught: normalization dropped (no view, no split
      join); labelled values applied on model input; the PEM pass dropped
      from `card_args`; the Diff tab back on the v1 scrub

Every credential-shaped sample, and every `password`/`pwd`/`passphrase`
label beside a sample value, is assembled at run time from pieces (the
repository's convention, `inline_credentials_check.py`, `remote_api_check.py`):
no literal a secret scanner matches is committed. All are fake.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/scrub_layers_check.py
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import approvals, chat_reads, daemon, hud_api, redact  # noqa: E402
from jarvis.v2.approvals import ApprovalRequest  # noqa: E402
from jarvis.v2.discord import mirror, reporter  # noqa: E402
from tests.v2 import diff_review_check as DR  # noqa: E402
from tests.v2.remote_api_check import LABELLED_CODE, LABELLED_KEEP, LABELLED_PROBE_WITHHOLD, SAMPLES  # noqa: E402

MARK = "[withheld: "


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pem(label: str, rng: random.Random, lines: int = 5) -> tuple[str, list[str]]:
    """A fake private-key block and its body lines (random base64, never a key)."""
    body = base64.b64encode(rng.randbytes(48 * lines)).decode()
    rows = [body[i:i + 64] for i in range(0, len(body), 64)]
    head, foot = "-----BEGIN " + label + "-----", "-----END " + label + "-----"
    return "\n".join([head, *rows, foot]), rows


AWS_ID = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"
AWS_SECRET = SAMPLES["AWS_SECRET"]
PW = "pass" + "word"
PW_VALUE = "S3cr" + "etPass" + "9x"

# (name, text, secret pieces that must never be shown). Each text sits in a
# sentence: "before <text> after".
SHAPES = [
    ("GitHub classic", "gh" + "p_" + "Zq7Kp2" * 6, None),
    ("GitHub OAuth", "gh" + "o_" + "a1B2c3" * 6, None),
    ("GitHub user-to-server", "gh" + "u_" + "d4E5f6" * 6, None),
    ("GitHub server-to-server", "gh" + "s_" + "g7H8i9" * 6, None),
    ("GitHub refresh", "gh" + "r_" + "j1K2l3" * 6, None),
    ("GitHub fine-grained", "github" + "_pat_" + "11ABCDEFG0" + "x9" * 30, None),
    ("OpenRouter", "sk-" + "or-v1-" + "0a" * 24, None),
    ("OpenAI project", "sk-" + "proj-" + "Ab3" * 12, None),
    ("Anthropic", "sk-" + "ant-api03-" + "Xy9" * 12, None),
    ("AWS key id", AWS_ID, None),
    ("AWS key id and secret", "id " + AWS_ID + " secret " + AWS_SECRET, [AWS_ID, AWS_SECRET]),
    ("AWS secret, labelled", "aws_secret_access_key = " + AWS_SECRET, [AWS_SECRET]),
    ("AWS secret, env", "AWS_SECRET_ACCESS_KEY=" + AWS_SECRET, [AWS_SECRET]),
    ("AWS secret, JSON", '"SecretAccessKey": "' + AWS_SECRET + '"', [AWS_SECRET]),
    ("AWS session token", SAMPLES["AWS_SESSION"], None),
    ("JWT", ".".join((_b64(b'{"alg":"HS256","typ":"JWT"}'), _b64(b'{"sub":"user-12345","n":"x"}'),
                      _b64(b"signature-bytes-0123456789"))), None),
    ("Bearer", "Authorization: " + "Bear" + "er " + "abc123DEF456ghi789jkl", ["abc123DEF456ghi789jkl"]),
    ("Basic", "Authorization: " + "Bas" + "ic " + base64.b64encode(("deploy:" + PW_VALUE).encode()).decode(),
     [base64.b64encode(("deploy:" + PW_VALUE).encode()).decode()]),
    ("URL password", "postgres://bob:" + PW_VALUE + "@db.internal.corp/app", [PW_VALUE]),
    ("Discord bot", "MTk4NjIyNDgzNDcxOTI1MjQ4" + "." + "Cl2FMQ" + "." + "ZnCjm1XVW7vRze4b7Cq4se7kKWs", None),
    ("Google", "AI" + "za" + "SyA1234567890" + "abcdefghijklmnopqrstuv", None),
    ("Stripe live", "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc", None),
    ("Stripe restricted", "rk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc", None),
    ("npm", "np" + "m_" + "a1B2c3D4e5" * 3 + "f6G7h8", None),
    ("Hugging Face", "h" + "f_" + "aBcDeFgHiJ" * 3 + "kLmN", None),
    ("Slack bot", "xo" + "xb-" + "1234567890-" + "abcdefghij", None),
    ("Slack user", "xo" + "xp-" + "1234567890-" + "abcdefghij", None),
    ("Slack app", "xo" + "xa-" + "1234567890-" + "abcdefghij", None),
    ("Slack refresh", "xo" + "xr-" + "1234567890-" + "abcdefghij", None),
    ("Slack session", "xo" + "xs-" + "1234567890-" + "abcdefghij", None),
    ("GitLab", "gl" + "pat-" + "Ab3De6Gh9Jk2Mn5Pq8St", None),
    ("Jarvis caller", "jm" + "cp_" + "abcdef0123", None),
]
_RNG = random.Random(0x5C2B)
for _label in ("RSA PRIVATE KEY", "EC PRIVATE KEY", "DSA PRIVATE KEY", "OPENSSH PRIVATE KEY",
               "ENCRYPTED PRIVATE KEY", "PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
    _block, _rows = _pem(_label, _RNG)
    SHAPES.append(("private key: " + _label, _block, _rows))

# The prefixed shapes a split, fullwidth or combining variant is made of.
PREFIXED = [name for name in ("GitHub classic", "GitHub fine-grained", "OpenRouter", "OpenAI project",
                              "Anthropic", "Google", "Stripe live", "npm", "Hugging Face", "Slack bot",
                              "GitLab", "Jarvis caller")]
SPLITS = {"NBSP": "\xa0", "tab": "\t", "newline": "\n", "U+2028": "\u2028", "U+0085": "\u0085"}
INSERTS = {"combining acute": "\u0301", "zero-width space": "\u200b", "soft hyphen": "\xad",
           "bidi override": "\u202e", "word joiner": "\u2060"}

# Ordinary code, hashes and prose that no path may change (on top of PR
# #308's keep set).
EXTRA_KEEP = [
    "commit 3f2a9c1e0b7d4a6f8e9c0b1a2d3e4f5a6b7c8d9e (HEAD -> main)",
    "id = 123e4567-e89b-12d3-a456-426614174000",
    "image: registry.example.invalid/app@sha256:" + "ab12cd34" * 8,
    "![chart](data:image/png;base64," + base64.b64encode(random.Random(5).randbytes(600)).decode() + ")",
    'headers = {"Authorization": "Bearer " + token}',
    'headers = {"Authorization": f"Bearer {token}"}',
    "Bearer authentication is described in RFC 6750.",
    "DATABASE_URL=postgres://app:${DB_" + "PASS" + "WORD}@db:5432/app",
    "url = 'https://user:<" + PW + ">@example.invalid'",
    "The ghp_ prefix is followed by thirty six characters of base62.",
    'token = os.environ["GITHUB_TOKEN"]',
    "from sklearn.model_selection import train_test_split",
    "An AKIA id pairs with a secret: commit 3f2a9c1e0b7d4a6f8e9c0b1a2d3e4f5a6b7c8d9e is lowercase hex.",
    "eyJ is how a base64 JSON header starts.",
    "ssh-ed25519 " + base64.b64encode(random.Random(6).randbytes(51)).decode() + " me@box",
    "中文的说明 🔑 ok, café naïve (decomposed: cafe\u0301), 👩\u200d💻 at work",
    "def rotate(key_id: str, *, dry_run: bool = False) -> None:",
    "Pass `api_key=settings.API_KEY` (or `client_secret=CLIENT_SECRET`) to the client.",
    "client = boto3.client('s3', aws_access_key_id=key_id, aws_secret_access_key=secret)",
]


def no_env():
    return (patch.object(secrets, "secret_values", return_value=[]),
            patch.object(secrets, "_process_env_values", return_value=()))


def _mirror(text):
    return mirror._scrub(text)


def _reporter(text):
    return reporter._clean(text)


MODEL_PATHS = {
    "redact.for_model": redact.for_model,
    "redact.for_model_strict (diff review input)": redact.for_model_strict,
    "redact.scrub_text": redact.scrub_text,
    "chat_reads.scrub_for_model (chat relay)": chat_reads.scrub_for_model,
    "chat_reads.scrub_read (another chat's rows)": chat_reads.scrub_read,
}
DISPLAY_PATHS = {
    "redact.for_display": redact.for_display,
    "chat_reads.scrub (transcript, phone, inbox)": chat_reads.scrub,
    "hud_api._scrub_source (desk Diff tab)": hud_api._scrub_source,
    "Discord mirror": _mirror,
    "Discord reporter": _reporter,
}
# A path that runs `secrets.redact_inline` first may withhold a value with
# its older `[redacted]` marker, and takes a URL's userinfo out whole with no
# marker at all (`strip_url_credentials`, #205; listed as an open item).
MARKED = ("[redacted", MARK)
INLINE_PATHS = ("chat_reads.scrub_read (another chat's rows)", "chat_reads.scrub (transcript, phone, inbox)",
                "card: a Write's content", "card: a command", "card: a command argument")


def _pieces(name, text, secret):
    return secret if secret is not None else [text]


# -- the checks (each returns its failures, so a mutation can be shown caught) ----------


def shape_failures(paths=None) -> list[str]:
    failures = []
    for path, fn in (paths or {**MODEL_PATHS, **DISPLAY_PATHS}).items():
        for name, text, secret in SHAPES:
            out = fn("before " + text + " after")
            for piece in _pieces(name, text, secret):
                if piece in out:
                    failures.append(f"{path}: {name} shown")
            if not any(m in out for m in MARKED) and not (name == "URL password" and path in INLINE_PATHS):
                failures.append(f"{path}: {name} not marked")
    return failures


_PREFIX = __import__("re").compile(
    r"sk-(?:or-|proj-|ant-)?|gh[pousr]_|github_pat_|AIza|sk_live_|npm_|hf_|xox[a-z]-|glpat-|jmcp_")
# An ordinary break (tab, newline) is joined only after a prefix whose first
# piece is not a whole token yet; an unusual one (NBSP, U+2028, U+0085)
# wherever it falls, inside the prefix or after a whole token.
UNUSUAL = ("NBSP", "U+2028", "U+0085")


def variants() -> list[tuple[str, str, list[str]]]:
    """(label, text, pieces that must not be shown)."""
    named = {name: text for name, text, _ in SHAPES}
    out = []
    for name in PREFIXED:
        token = named[name]
        half = len(token) // 2
        early = _PREFIX.match(token).end() + 3
        for split, ch in SPLITS.items():
            cuts = (early, 3, half) if split in UNUSUAL else (early,)
            for cut in cuts:
                out.append((f"{name} split by {split} at {cut}", token[:cut] + ch + token[cut:],
                            [token[:cut], token[cut:]] if cut > 3 else [token[cut:]]))
        wide = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in token)
        out.append((f"{name} fullwidth", wide, [wide, wide[:half], wide[half:]]))
        for insert, ch in INSERTS.items():
            text = token[:8] + ch + token[8:]
            out.append((f"{name} with a {insert}", text, [token[8:], text]))
    return out


def variant_failures(paths=None) -> list[str]:
    failures = []
    for path, fn in (paths or {**MODEL_PATHS, **DISPLAY_PATHS}).items():
        for label, text, pieces in variants():
            out = fn("before " + text + " after")
            if any(piece in out for piece in pieces):
                failures.append(f"{path}: {label} shown")
            elif not any(m in out for m in MARKED):
                failures.append(f"{path}: {label} not marked")
    return failures


def faithful_failures() -> list[str]:
    """`for_model` changes nothing but the withheld span: the text around a
    normalized-match variant comes back byte for byte."""
    failures = []
    for label, text, _pieces_ in variants():
        out = redact.for_model("before \xa0é\u200b " + text + " after é\u0301")
        if not (out.startswith("before \xa0é\u200b " + MARK) and out.endswith("] after é\u0301")):
            failures.append(f"{label}: {out!r}")
    return failures


MODEL_KEEP = LABELLED_KEEP + [line for line in LABELLED_CODE if line not in LABELLED_KEEP] + EXTRA_KEEP
_AWS_PROBE = "aws_secret_access_key = " + AWS_SECRET
PROBES = [line for line in LABELLED_PROBE_WITHHOLD if line != _AWS_PROBE]


def model_keep_failures() -> list[str]:
    """The keep set and the review's labelled probe lines, unchanged on every
    model-input path."""
    failures = []
    for line in MODEL_KEEP + PROBES:
        for path, fn in {"redact.for_model": redact.for_model,
                         "chat_reads.scrub_for_model": chat_reads.scrub_for_model}.items():
            if fn(line) != line:
                failures.append(f"{path} changed {line!r} -> {fn(line)!r}")
    return failures


def card_failures() -> list[str]:
    failures = []
    rng = random.Random(0xCA2D)
    block, rows = _pem("OPENSSH PRIVATE KEY", rng)
    for args in ({"file_path": "deploy_key", "content": block + "\n"},
                 {"file_path": "notes.md", "old_string": "x", "new_string": "the key:\n" + block + "\nend"},
                 {"file_path": "list.txt", "content": ["a", block]}):
        shown = json.dumps(approvals.card_args(args))
        published = json.dumps(ApprovalRequest(tool="Write", args=args).to_json()["args"])
        logged = json.dumps(daemon._log_fields({"name": "Write", "args": args})["args"])
        for out in (shown, published, logged):
            if any(row in out for row in rows):
                failures.append(f"card_args shows a key body: {sorted(args)}")
            if "[withheld: private key]" not in out:
                failures.append(f"card_args does not mark the key: {sorted(args)}")
    return failures


# -- the tests --------------------------------------------------------------------------


class Base(unittest.TestCase):
    def setUp(self):
        for patcher in no_env():
            patcher.start()
            self.addCleanup(patcher.stop)


class Shapes(Base):
    def test_s1_every_shape_is_withheld_on_every_path(self):
        self.assertEqual(shape_failures(), [])
        for name, text, _ in SHAPES:
            out = redact.for_model("before " + text + " after")
            self.assertTrue(out.startswith("before ") and out.endswith(" after"), (name, out))
            self.assertIn(MARK, out, name)
        print(f"ok  s1: {len(SHAPES)} credential shapes withheld and marked on "
              f"{len(MODEL_PATHS)} model-input and {len(DISPLAY_PATHS)} display paths")

    def test_s1_the_marker_names_the_kind(self):
        self.assertEqual(redact.shapes("t " + SHAPES[0][1]), "t [withheld: GitHub token]")
        self.assertEqual(redact.shapes("aws_secret_access_key = " + AWS_SECRET),
                         "aws_secret_access_key = [withheld: AWS secret key]")
        self.assertEqual(redact.shapes("postgres://bob:" + PW_VALUE + "@db/x"),
                         "postgres://bob:[withheld: URL password]@db/x")
        self.assertEqual(redact.for_display(PW + ": " + PW_VALUE), PW + ": [withheld: labelled secret]")
        self.assertEqual(redact.for_model(PW + ": " + PW_VALUE), PW + ": " + PW_VALUE)
        print("ok  s1: a marker says what was withheld; a labelled value only on display")

    def test_s1_scrubbing_twice_changes_nothing_more(self):
        for name, text, _ in SHAPES:
            once = redact.for_display("before " + text + " after")
            self.assertEqual(redact.for_display(once), once, name)
            self.assertEqual(redact.for_model(once), once, name)
        print("ok  s1: a scrubbed text scrubbed again is unchanged (a marker is never re-matched)")


class Variants(Base):
    def test_s2_split_fullwidth_and_combining_variants_are_withheld(self):
        self.assertEqual(variant_failures(), [])
        print(f"ok  s2: {len(variants())} split, fullwidth, combining, zero-width, soft-hyphen and bidi "
              f"variants of {len(PREFIXED)} shapes withheld on every path")

    def test_s2_normalization_is_for_matching_only(self):
        self.assertEqual(faithful_failures(), [])
        print("ok  s2: outside a withheld span the model-input text is byte for byte the input "
              "(NBSP, zero-width, combining marks kept)")

    def test_s2_a_split_needs_random_looking_pieces(self):
        for text in ("The ghp_ prefix\nis then followed by more words than a token has",
                     "sk-or- and\tthen the next sentence goes on at length here",
                     "eyJ is\xa0the start of a JSON web token, explained"):
            self.assertEqual(redact.for_model(text), text)
        print("ok  s2: a prefix followed by words across a break is left alone")


class KeepSet(Base):
    def test_s3_the_keep_set_reaches_a_model_unchanged(self):
        self.assertEqual(model_keep_failures(), [])
        print(f"ok  s3: {len(MODEL_KEEP)} code, config, hash and prose lines (PR #308's "
              f"{len(LABELLED_KEEP)} and its {len(LABELLED_CODE)} code lines among them) and "
              f"{len(PROBES)} labelled probe lines unchanged on the model-input paths")

    def test_s3_the_keep_set_is_unchanged_on_display_too(self):
        for line in MODEL_KEEP:
            self.assertEqual(redact.for_display(line), line, line)
            self.assertEqual(hud_api._scrub_source(line + "\n"), line + "\n", line)
        print(f"ok  s3: the {len(MODEL_KEEP)} keep lines unchanged by the display scrub and the Diff tab")

    def test_s3_labelled_values_are_withheld_on_display_only(self):
        caught = [line for line in PROBES if redact.for_display(line) != line]
        self.assertGreaterEqual(len(caught), 5)
        for line in caught:
            self.assertIn("[withheld: labelled secret]", redact.for_display(line), line)
        print(f"ok  s3: {len(caught)} of the {len(PROBES)} probe lines withheld on display, none on model input")


class DiffTab(DR.Base):
    def setUp(self):
        super().setUp()
        for patcher in no_env():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.block, self.rows = _pem("RSA PRIVATE KEY", random.Random(0xD1FF))
        self.token = SHAPES[0][1]
        self.labelled = PW + ": " + PW_VALUE
        (self.wt / "settings.py").write_text(
            "GITHUB = '" + self.token + "'\n" + self.block + "\n" + self.labelled + "\n"
            + "client = Client(api_key=settings.API_KEY)\n")

    def test_s4_the_diff_route_withholds_shapes_and_labelled_values(self):
        patch_ = self.request("GET", f"/tasks/{self.task.id}/diff")["patch"]
        after = self.request("GET", f"/tasks/{self.task.id}/diff/file?path=settings.py")["after"]
        for shown in (patch_, after):
            self.assertNotIn(self.token, shown)
            for row in self.rows:
                self.assertNotIn(row, shown)
            self.assertNotIn(PW_VALUE, shown)
            self.assertIn("[withheld: GitHub token]", shown)
            self.assertIn("[withheld: private key]", shown)
            self.assertIn(PW + ": [withheld: labelled secret]", shown)
            self.assertIn("client = Client(api_key=settings.API_KEY)", shown)
        self.assertTrue(after.endswith("\n"))
        print("ok  s4: the desk's Diff tab (patch and file) withholds a GitHub token, a private key "
              "and a labelled value; ordinary code stays")

    def test_s4_diff_reviews_model_input_takes_the_shapes_only(self):
        from jarvis.v2 import diff_review
        text = diff_review.build_input(self.daemon, self.task).diff_text
        self.assertNotIn(self.token, text)
        for row in self.rows:
            self.assertNotIn(row, text)
        self.assertIn(self.labelled, text)
        self.assertIn("client = Client(api_key=settings.API_KEY)", text)
        print("ok  s4: diff review's model input withholds the shapes and keeps the labelled line as written")


class Cards(Base):
    def test_s5_a_private_key_in_a_write_is_withheld_whole(self):
        self.assertEqual(card_failures(), [])
        print("ok  s5: a private key in a Write's content, an Edit's new_string or a list is withheld "
              "whole on the card and its published args")

    def test_s5_cards_stay_faithful(self):
        for command in ("git status", "ls -la ~/projects", "pytest -q tests/v2",
                        "sed -i 's/" + PW + ": .*/" + PW + ": REDACTED/' config.yml",
                        "kubectl create secret generic db --from-literal=" + PW + "=$DB_PASS",
                        "grep -rn 'api_key' src/ | head -20"):
            self.assertEqual(approvals.card_text(command), command)
            self.assertEqual(approvals.card_args({"command": command}), {"command": command})
        # A line that re-parses text as code keeps a labelled value (rule 3:
        # the value may be code); the card adds nothing to redact_command's.
        for command in ('eval "' + PW + ': ' + PW_VALUE + '"',
                        "bash -c 'echo " + PW + "=" + PW_VALUE + "'",
                        "echo " + PW + ": " + PW_VALUE + " | sh"):
            self.assertEqual(approvals.card_text(command), secrets.redact_command(command), command)
            self.assertIn(PW_VALUE, approvals.card_text(command), command)
        # A key at a command's place runs as commands: it stays, as rule 3 says.
        block, rows = _pem("PRIVATE KEY", random.Random(9))
        self.assertIn(rows[1], approvals.card_text(block))
        self.assertIn(rows[1], approvals.card_args({"command": block})["command"])
        # The new shapes reach a command's card too, where no structure is hidden.
        wide = "".join(chr(ord(c) + 0xFEE0) for c in SHAPES[0][1])
        self.assertEqual(approvals.card_text("echo " + wide), "echo [withheld: GitHub token]")
        # A Write's content takes the display scrub's labelled values.
        shown = approvals.card_args({"file_path": "c.yml", "content": self.__class__.labelled()})
        self.assertNotIn(PW_VALUE, json.dumps(shown))
        print("ok  s5: ordinary commands are shown exactly; a key at a command's place stays; "
              "a fullwidth token and a labelled value in a Write are withheld")

    @staticmethod
    def labelled():
        return "db:\n  " + PW + ": " + PW_VALUE + "\n"

    def test_s5_every_shape_and_variant_in_a_write_is_withheld(self):
        def content(text):
            return json.dumps(approvals.card_args({"file_path": "x", "content": text}))
        self.assertEqual(shape_failures({"card: a Write's content": content}), [])
        self.assertEqual(variant_failures({"card: a Write's content": content}), [])
        print(f"ok  s5: all {len(SHAPES)} shapes and {len(variants())} variants withheld in a Write's content")

    def test_s5_a_command_card_withholds_what_hides_no_structure(self):
        """On a command, a private key stays (it runs at a command's place)
        and so does a token split by a tab or newline (rule 0: the span would
        hold a blank the shell splits on); everything else is withheld."""
        commands = {"card: a command": approvals.card_text,
                    "card: a command argument": lambda t: approvals.card_args({"command": t})["command"]}
        keys = {name for name, _t, _s in SHAPES if name.startswith("private key")}
        failures = [f for f in shape_failures(commands) if not any(f.endswith(f"{k} shown") or
                                                                  f.endswith(f"{k} not marked") for k in keys)]
        self.assertEqual(failures, [])
        shown = [f for f in variant_failures(commands)]
        self.assertTrue(all(" split by tab " in f or " split by newline " in f for f in shown), shown[:5])
        print(f"ok  s5: a command's card withholds every shape but a key at a command's place, and every "
              f"variant but the {len(shown) // len(commands)} split by a tab or newline (rule 0)")


class Mutations(Base):
    """Each mutation of the scrub is caught by the check named."""

    def caught(self, name, failures):
        self.assertTrue(failures, f"mutation not caught: {name}")
        print(f"ok  s6: mutation caught: {name} ({len(failures)} failures)")

    def test_s6_no_normalized_view(self):
        with patch.object(redact, "_SPECIAL", __import__("re").compile(r"(?!)")):
            self.caught("matching on the raw text (no fold, no dropped characters)", variant_failures())

    def test_s6_no_split_join(self):
        with patch.object(redact, "_split_spans", return_value=[]):
            self.caught("no join of a token split across a break", variant_failures())

    def test_s6_labelled_values_on_model_input(self):
        real = redact.for_model
        with patch.object(redact, "for_model", side_effect=lambda t: redact.labelled_values(real(t))):
            self.caught("labelled values applied on model input", model_keep_failures())

    def test_s6_no_private_key_pass_on_cards(self):
        real = redact.card_spans

        def no_keys(text, command=True):
            return [s for s in real(text, command=command) if s[2] != redact.KEY]
        with patch.object(redact, "card_spans", side_effect=no_keys):
            self.caught("the PEM pass dropped from card_args", card_failures())

    def test_s6_the_diff_tab_on_the_v1_scrub(self):
        def v1(text):
            return secrets.scrub(text) + ("\n" if text.endswith("\n") else "")
        with patch.object(hud_api, "_scrub_source", side_effect=v1):
            paths = {"hud_api._scrub_source": lambda t: hud_api._scrub_source(t)}
            self.caught("the Diff tab back on secrets.scrub", shape_failures(paths))


if __name__ == "__main__":
    unittest.main(verbosity=1)
