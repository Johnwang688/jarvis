"""Free checks: SEC-5, `scrub_text` redacts a private key's whole block (2026-10-19).

`redact.scrub_text` applies `credential_patterns.PATTERNS`, whose private-key
entries match only the `-----BEGIN … PRIVATE KEY-----` header and the
`-----END …-----` footer. The base64 body between them went out unredacted to
every surface `scrub_text` feeds (the Discord mirror and reporter, diff review,
chat git, the Codex provider, subagents, the daemon). The fix redacts the whole
block as one `[redacted]`, before the per-pattern pass, and is tighten-only:
nothing redacted before becomes unredacted, and text with no private-key
header or footer comes out byte-identical.

These are written to FAIL before the fix.

Fakes only: every key body is random base64 from a seeded RNG, built at run
time, never a real key; key-shaped tokens are built by concatenation.
`secrets.secret_values` and `secrets._process_env_values` are patched empty, so
no real credential file or environment value is read.

Cases:
  k1  RSA, OPENSSH, EC, ENCRYPTED (PKCS#8 and legacy Proc-Type/DEK-Info), PGP
      and plain PKCS#8 blocks mid-text: one `[redacted]`, surroundings unchanged
  k2  an unterminated block: from its header through its key lines (to the
      end of the text when nothing else follows; third review of #314)
  k3  a footer-only tail: the key-material lines above the footer, back to the
      first line that is not one (and no further)
  k4  two blocks in one text stay two blocks (non-greedy)
  k5  a public key and a certificate are unchanged, as today
  k6  ordinary base64 in prose, with no header or footer, is unchanged
  k7  tighten-only: random texts with no header/footer are byte-identical to
      the old scrub; no token the old scrub redacted is shown by the new one
  k8  1 MiB adversarial inputs finish within a CPU bound, in a child process
      with an active deadline (a catastrophic regex cannot be interrupted
      from its own thread)

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/redact_private_key_check.py
"""
from __future__ import annotations

import base64
import difflib
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import credential_patterns  # noqa: E402
from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import redact  # noqa: E402

R = "[redacted]"
BEFORE = "Here is the key you asked about:\n"
AFTER = "\nCan you tell me why ssh refuses it?"

# The CPU-seconds bound for one 1 MiB case. The whole scrub took about 0.1 s
# per case before the fix (2026-10-19, this machine); a quadratic pass over
# 1 MiB takes minutes. The child's wall deadline is the active guard.
CPU_BOUND = 2.0
CHILD_DEADLINE = 240


def b64_lines(rng: random.Random, n: int, width: int = 64, last: int = 0) -> list[str]:
    """`n` lines of random base64 `width` wide, and a short padded last line
    if `last` (bytes) is set: a fake key body, never a real key."""
    raw = rng.randbytes(width * 3 // 4 * n)
    text = base64.b64encode(raw).decode()
    out = [text[i:i + width] for i in range(0, len(text), width)]
    if last:
        out.append(base64.b64encode(rng.randbytes(last)).decode())
    return out


def header(label: str) -> str:
    return "-----BEGIN " + label + "-----"


def footer(label: str) -> str:
    return "-----END " + label + "-----"


def block(rng: random.Random, label: str, extra: list[str] = (), n: int = 6) -> str:
    return "\n".join([header(label), *extra, *b64_lines(rng, n, last=5), footer(label)])


def pgp_block(rng: random.Random) -> str:
    label = "PGP PRIVATE KEY BLOCK"
    body = b64_lines(rng, 5, width=64, last=7)
    checksum = "=" + base64.b64encode(rng.randbytes(3)).decode()
    return "\n".join([header(label), "Version: GnuPG v2", "", *body, checksum, footer(label)])


def ghp() -> str:
    return "ghp_" + "Q" * 36


def sk() -> str:
    return "sk-or-" + "v1" + "7" * 30


def old_scrub(text: str) -> str:
    """The scrub as it was before SEC-5: the oracle for tighten-only."""
    out = secrets.scrub(str(text or ""))
    for _name, pattern in credential_patterns.PATTERNS:
        out = pattern.sub(R, out)
    return redact.bearerish(out)


# Since the scrub hardening (2026-10-25) a withheld span says what it was
# (`[withheld: private key]`, `[withheld: GitHub token]`); these checks pin
# *where* the scrub withholds, so every marker is read as the old one.
MARKER = re.compile(r"\[withheld: [^\]\n]{1,40}\]")


def scrubbed(text: str) -> str:
    """`redact.scrub_text`, every typed marker read as `[redacted]`."""
    return MARKER.sub(R, redact.scrub_text(text))


def no_env():
    """No real credential file or environment value reaches the scrub."""
    return (patch.object(secrets, "secret_values", return_value=[]),
            patch.object(secrets, "_process_env_values", return_value=()))


class Base(unittest.TestCase):
    def setUp(self):
        for patcher in no_env():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rng = random.Random(0x5EC5)

    def assertWhole(self, text: str, want: str):
        got = scrubbed(text)
        self.assertEqual(got, want)


class BlockChecks(Base):
    def test_k1_each_kind_of_private_key_is_one_redaction(self):
        """k1: the body goes with the header and footer, and nothing else does."""
        kinds = {
            "RSA": block(self.rng, "RSA PRIVATE KEY"),
            "OPENSSH": block(self.rng, "OPENSSH PRIVATE KEY", n=9),
            "EC": block(self.rng, "EC PRIVATE KEY", n=3),
            "PKCS#8": block(self.rng, "PRIVATE KEY"),
            "ENCRYPTED": block(self.rng, "ENCRYPTED PRIVATE KEY"),
            "legacy encrypted": block(self.rng, "RSA PRIVATE KEY",
                                      extra=["Proc-Type: 4,ENCRYPTED",
                                             "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF",
                                             ""]),
            "DSA": block(self.rng, "DSA PRIVATE KEY"),
            "PGP": pgp_block(self.rng),
        }
        for name, key in kinds.items():
            with self.subTest(kind=name):
                got = scrubbed(BEFORE + key + AFTER)
                self.assertEqual(got, BEFORE + R + AFTER)
                self.assertEqual(got.count(R), 1)
                for body_line in key.splitlines()[1:-1]:
                    if len(body_line) >= 8:
                        self.assertNotIn(body_line, got)

    def test_k1_a_block_on_a_line_with_other_text(self):
        """k1: text on the header's and footer's lines outside the markers stays."""
        key = block(self.rng, "RSA PRIVATE KEY")
        self.assertWhole("key: " + key + " (end)", "key: " + R + " (end)")

    def test_k1_crlf_line_endings(self):
        key = block(self.rng, "OPENSSH PRIVATE KEY").replace("\n", "\r\n")
        self.assertWhole(BEFORE + key + AFTER, BEFORE + R + AFTER)

    def test_k2_an_unterminated_block_runs_to_the_end(self):
        """k2: a header with no footer: redacted through its key lines, to the
        end of the text when nothing else follows. Since the third review of
        #314 (H, L5) a line after them that is not key material (prose, code)
        stays, as a fake header can sit above code that runs."""
        body = "\n".join(b64_lines(self.rng, 8))
        self.assertWhole(BEFORE + header("RSA PRIVATE KEY") + "\n" + body + AFTER, BEFORE + R + AFTER)
        self.assertWhole(header("OPENSSH PRIVATE KEY") + "\n" + body, R)
        # Any private-key footer ends the block, whatever its kind; what
        # follows it stays.
        text = header("EC PRIVATE KEY") + "\n" + body + "\n" + footer("RSA PRIVATE KEY") + AFTER
        self.assertWhole(text, R + AFTER)

    def test_k3_a_footer_only_tail(self):
        """k3: a read that starts below the header: the body above the footer goes."""
        body = "\n".join(b64_lines(self.rng, 7, last=4))
        tail = footer("RSA PRIVATE KEY")
        # From the start of the text.
        self.assertWhole(body + "\n" + tail + AFTER, R + AFTER)
        # Below a line that is not key material: that line and above stay.
        self.assertWhole("$ tail -8 id_rsa\n" + body + "\n" + tail + AFTER,
                         "$ tail -8 id_rsa\n" + R + AFTER)
        # PEM header lines count as key material.
        legacy = "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00112233445566778899AABBCCDDEEFF\n"
        self.assertWhole("prose here\n" + legacy + body + "\n" + tail, "prose here\n" + R)
        # CRLF, and the body glued to the footer on its last line.
        # (Since the review of #314, S4, the scrub keeps the text's own line
        # endings: `secrets.scrub`'s LF-joined lines are put back with them.)
        crlf = (body + "\n" + tail).replace("\n", "\r\n")
        self.assertWhole("words here\r\n" + crlf + AFTER, "words here\r\n" + R + AFTER)
        self.assertEqual(redact.private_key_blocks("words here\r\n" + crlf + "\r\nbye"),
                         "words here\r\n" + R + "\r\nbye")
        glued = body + tail
        self.assertWhole("words here\n" + glued + AFTER, "words here\n" + R + AFTER)
        # A PGP tail: the checksum line (`=` + 4) is key material too.
        pgp = pgp_block(self.rng).splitlines()
        self.assertWhole("gpg output:\n" + "\n".join(pgp[3:]) + AFTER, "gpg output:\n" + R + AFTER)

    def test_k3_the_footer_rule_stops_at_the_first_line_that_is_not_key_material(self):
        """k3: conservative: a blank line or a line of words ends the walk back."""
        top = "\n".join(b64_lines(self.rng, 3))
        body = "\n".join(b64_lines(self.rng, 4, last=4))
        tail = footer("OPENSSH PRIVATE KEY")
        text = "intro words\n" + top + "\nnot key material\n" + body + "\n" + tail + AFTER
        self.assertWhole(text, "intro words\n" + top + "\nnot key material\n" + R + AFTER)
        text = top + "\n\n" + body + "\n" + tail
        self.assertWhole(text, top + "\n\n" + R)
        # A footer straight after prose: only the footer, exactly as before.
        text = "I deleted the key, this is what was left:\n" + tail + AFTER
        self.assertWhole(text, "I deleted the key, this is what was left:\n" + R + AFTER)
        self.assertEqual(scrubbed(text), old_scrub(text))
        # Words before the footer on its own line: that line is not key
        # material, so nothing above it goes either; only the footer, as before.
        text = body + "\nsee: " + tail + AFTER
        self.assertWhole(text, body + "\nsee: " + R + AFTER)
        self.assertEqual(scrubbed(text), old_scrub(text))

    def test_k4_two_blocks_stay_two(self):
        """k4: non-greedy: the text between two blocks stays."""
        one = block(self.rng, "RSA PRIVATE KEY")
        two = block(self.rng, "OPENSSH PRIVATE KEY")
        middle = "\nand the second one, for the deploy box:\n"
        got = scrubbed(BEFORE + one + middle + two + AFTER)
        self.assertEqual(got, BEFORE + R + middle + R + AFTER)
        # And a block followed by an unterminated one.
        body = "\n".join(b64_lines(self.rng, 3))
        got = scrubbed(one + middle + header("EC PRIVATE KEY") + "\n" + body)
        self.assertEqual(got, R + middle + R)
        # And a block followed by a footer-only tail.
        got = scrubbed(one + "\n" + body + "\n" + footer("EC PRIVATE KEY") + AFTER)
        self.assertEqual(got, R + "\n" + R + AFTER)

    def test_k5_public_keys_and_certificates_are_unchanged(self):
        """k5: not private: exactly as today (unchanged)."""
        for label in ("PUBLIC KEY", "RSA PUBLIC KEY", "CERTIFICATE", "CERTIFICATE REQUEST",
                      "PGP PUBLIC KEY BLOCK", "X509 CRL"):
            with self.subTest(label=label):
                text = BEFORE + block(self.rng, label) + AFTER
                self.assertEqual(scrubbed(text), text)
                self.assertEqual(scrubbed(text), old_scrub(text))
        ssh = "ssh-ed25519 " + base64.b64encode(self.rng.randbytes(51)).decode() + " me@box"
        self.assertEqual(scrubbed(ssh), ssh)
        # A certificate chain with a key after it: the certificates stay.
        cert = block(self.rng, "CERTIFICATE")
        key = block(self.rng, "PRIVATE KEY")
        self.assertWhole(cert + "\n" + cert + "\n" + key + AFTER, cert + "\n" + cert + "\n" + R + AFTER)
        # A footer-only tail below a certificate: the walk stops at its footer.
        body = "\n".join(b64_lines(self.rng, 4))
        self.assertWhole(cert + "\n" + body + "\n" + footer("PRIVATE KEY"), cert + "\n" + R)

    def test_k6_ordinary_base64_is_unchanged(self):
        """k6: no header or footer, nothing new is redacted."""
        blob = base64.b64encode(self.rng.randbytes(300)).decode()
        texts = [
            "The attachment decodes from " + blob + " and is a PNG.",
            "Lines:\n" + "\n".join(b64_lines(self.rng, 12)) + "\nend of blob",
            "\n".join(b64_lines(self.rng, 12)),
            "data:image/png;base64," + blob,
            "the BEGIN and END of the PRIVATE KEY section, explained in words",
            "----- BEGIN RSA PRIVATE KEY ----- is not the header (spaces)",
        ]
        for text in texts:
            with self.subTest(text=text[:30]):
                self.assertEqual(scrubbed(text), text)

    def test_k7_other_key_shapes_are_still_redacted_around_a_block(self):
        """k7: the per-pattern and bearer passes still run on what is left."""
        key = block(self.rng, "RSA PRIVATE KEY")
        text = ghp() + " " + BEFORE + key + AFTER + " " + sk()
        self.assertWhole(text, R + " " + BEFORE + R + AFTER + " " + R)

    def test_k7_a_token_glued_to_a_header_is_not_unredacted(self):
        """k7: the old scrub redacted `sk-or-…` reading on into `-----BEGIN`;
        the token's own characters must not come back once the block goes."""
        short = "sk-or-" + "Z" * 15             # too short alone: only the old glued run matched
        key = block(self.rng, "RSA PRIVATE KEY")
        text = "x " + short + key + AFTER
        self.assertIn(R, old_scrub(text))
        self.assertNotIn(short, old_scrub(text))
        got = scrubbed(text)
        self.assertNotIn(short, got)
        self.assertNotIn("Z" * 15, got)
        self.assertEqual(got, "x " + R + AFTER)


class TightenOnly(Base):
    def pieces(self):
        rng = self.rng
        words = ["the", "deploy", "key", "is", "below", "thanks", "ok", "BEGIN", "END",
                 "PRIVATE", "KEY", "-----", "--", "=", "==", ":", "Proc-Type: 4,ENCRYPTED"]
        tokens = [ghp(), sk(), "sk-or-" + "Z" * 15, "AKIA" + "T" * 16, "xoxb-" + "1" * 12,
                  "glpat-" + "g" * 20, "jmcp_" + "m" * 10, "hf_" + "h" * 32]
        glue = ["", " ", "\n", "\r\n", "\n\n", "\t"]
        marks = [header("RSA PRIVATE KEY"), footer("RSA PRIVATE KEY"), header("PRIVATE KEY"),
                 footer("OPENSSH PRIVATE KEY"), header("PUBLIC KEY"), footer("CERTIFICATE")]
        return words, tokens, glue, marks

    def random_text(self, rng, words, tokens, glue, marks, with_marks):
        parts = []
        for _ in range(rng.randrange(1, 18)):
            kind = rng.random()
            if kind < 0.35:
                parts.append(rng.choice(words))
            elif kind < 0.55:
                parts.append(rng.choice(tokens))
            elif kind < 0.75:
                parts.append(b64_lines(rng, 1, width=rng.choice((4, 16, 64)))[0])
            elif kind < 0.85 and with_marks:
                parts.append(rng.choice(marks))
            parts.append(rng.choice(glue))
        return "".join(parts)

    def test_k7_no_header_or_footer_means_byte_identical(self):
        """k7: 3000 random texts with no private-key marker: the old output,
        or one that withholds more and nothing else. The scrub hardening
        (2026-10-25) withholds more on purpose (a token split across a break,
        an AWS secret beside a key id, a token glued to another withheld
        one), so byte identity became: every difference from the old output
        is old text replaced by markers; no character the old output hid or
        changed is shown, and no shown character is altered."""
        words, tokens, glue, marks = self.pieces()
        rng = random.Random(7)
        names = {"private-key", "private-key-end"}
        compared = differs = 0
        for _ in range(3000):
            text = self.random_text(rng, words, tokens, glue, marks, with_marks=False)
            if any(p.search(text) for n, p in credential_patterns.PATTERNS if n in names):
                continue
            compared += 1
            # Lines compared as `secrets.scrub` joins them: the scrub now keeps
            # the text's own line endings (review of #314, S4).
            old, new = old_scrub(text), "\n".join(scrubbed(text).splitlines())
            if new != old:
                differs += 1
                self.assertOnlyMoreHidden(old, new, text)
            for token in tokens:
                if token in text and token not in old:
                    self.assertNotIn(token, new, repr(text))
        self.assertGreater(compared, 2500)
        print(f"ok  k7: {compared} texts without a key marker: {compared - differs} exactly the old "
              f"output, {differs} withhold more and change nothing else")

    def assertOnlyMoreHidden(self, old: str, new: str, text: str):
        """Every difference between `old` and `new` is text of `old` that
        `new` replaced by markers: aligned with each marker as one symbol,
        no new-side piece holds anything but markers."""
        a, b = old.replace(R, "\x00"), new.replace(R, "\x00")
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                self.assertEqual(b[j1:j2].strip("\x00"), "", (repr(text), repr(a[i1:i2]), repr(b[j1:j2])))

    def test_k7_nothing_the_old_scrub_hid_is_shown(self):
        """k7: 3000 random texts with markers: every token the old output did
        not show, the new output does not show either."""
        words, tokens, glue, marks = self.pieces()
        rng = random.Random(8)
        for _ in range(3000):
            text = self.random_text(rng, words, tokens, glue, marks, with_marks=True)
            old, new = old_scrub(text), scrubbed(text)
            for token in tokens:
                if token in text and token not in old:
                    self.assertNotIn(token, new, repr(text))
            # A hidden header or footer stays hidden too.
            for mark in marks[:4]:
                if mark not in old:
                    self.assertNotIn(mark, new, repr(text))


# --------------------------------------------------------------------------
# k8: 1 MiB adversarial inputs, in a child with an active deadline

MIB = 1 << 20


def timing_cases(rng: random.Random) -> dict[str, str]:
    def fill(unit: str) -> str:
        return (unit * (MIB // len(unit) + 1))[:MIB]

    head, foot = header("RSA PRIVATE KEY"), footer("RSA PRIVATE KEY")
    lines = "\n".join(b64_lines(rng, MIB // 65))
    one = b64_lines(rng, 1)[0]
    return {
        "base64 only": lines,
        "base64 then a footer": lines + "\n" + foot,
        "a header then base64": head + "\n" + lines,
        "headers only": fill(head + "\n"),
        "footers only": fill(foot + "\n"),
        "blocks": fill(head + "\n" + one + "\n" + foot + "\n"),
        "a line and a footer": fill(one + "\n" + foot + "\n"),
        "header prefix, words": "-----BEGIN " + fill("A "),
        "footer prefix, words": "-----END " + fill("A "),
        "header prefixes": fill("-----BEGIN A "),
        "footer prefixes": fill("-----END AB "),
        "glued runs before headers": fill("A" * 1000 + head + "x" + foot),
        "glued runs before footers": fill("A" * 5000 + foot),
        "words glued to footers": fill("a b" + foot),
        "short lines before footers": fill("A\n" * 300 + foot),
        "dashes": fill("-"),
        "one long line": fill("Ab0+/"),
    }


def timing_child() -> int:
    for patcher in no_env():
        patcher.start()
    failed = 0
    for name, text in timing_cases(random.Random(11)).items():
        start = time.thread_time()
        redact.scrub_text(text)
        spent = time.thread_time() - start
        verdict = "ok  " if spent <= CPU_BOUND else "SLOW"
        failed += spent > CPU_BOUND
        print(f"{verdict} {name}: {spent:.3f}s cpu", flush=True)
    return 1 if failed else 0


class Timing(unittest.TestCase):
    def test_k8_one_mib_inputs_are_bounded(self):
        """k8: no catastrophic backtracking: each 1 MiB case within CPU_BOUND."""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        try:
            proc = subprocess.run([sys.executable, __file__, "--timing-child"], env=env,
                                  capture_output=True, text=True, timeout=CHILD_DEADLINE)
        except subprocess.TimeoutExpired as exc:
            self.fail(f"the 1 MiB cases did not finish within {CHILD_DEADLINE}s: "
                      f"{(exc.stdout or b'')[-400:]!r}")
        print(proc.stdout, end="", file=sys.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr[-2000:])


if __name__ == "__main__":
    if sys.argv[1:] == ["--timing-child"]:
        raise SystemExit(timing_child())
    unittest.TestCase.shortDescription = lambda self: None
    unittest.main(verbosity=2)
