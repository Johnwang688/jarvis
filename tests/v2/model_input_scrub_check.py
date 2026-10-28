"""Free checks: what a model reads arrives with ordinary code intact (review
of PR #314, S1, 2026-10-25).

Each model-input call site of the scrub is driven end to end and must hand
the model a block of keep lines unchanged: lines the display scrub rewrites
(`api_key=settings.API_KEY` becomes `api_key=[redacted]` under
`redact_inline`, `password: hunter2_2024` a labelled secret) and a model
must read as written.

  m1  a chat relay's body reaches the receiving chat's model unchanged
  m2  its reply reaches the sending chat's model unchanged
  m3  its failure reason reaches the sending chat's model unchanged
  m4  a chat git tool's output (`chat_git._render`) unchanged
  m5  a compaction reason (`daemon.compact_reason`) is the owner's, not a
      model's input (review of #314, M1): the display scrub, as main had it
  m6  mutations: each call site in turn put back on the display scrub (by
      its own source line, so the others stay right): each check fails; the
      compaction reason put on the model's scrub fails m5

Fakes only: `chat_relay_check`'s scripted providers, temporary stores and
config. The `password` label is assembled at run time (the repository's
convention for secret scanners).

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/v2/model_input_scrub_check.py
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.tools import secrets  # noqa: E402
from jarvis.v2 import chat_git, chat_reads, daemon, redact  # noqa: E402
from jarvis.v2.provider import Event, EventKind as K  # noqa: E402
from tests.v2 import chat_relay_check as CR  # noqa: E402

PW = "pass" + "word"
KEEP_LINES = [
    "client = Client(api_key=settings.API_KEY)",
    PW + ": hunter2_2024",
    "client_secret=CLIENT_SECRET,",
    PW + "=request.form['" + PW + "']",
    "refresh_token=refresh_token,",
]
KEEP = "\n".join(KEEP_LINES)
REASON = "api_key=settings.API_KEY was refused"


def _display(text):
    """The owner's scrub, as a mutation puts it where a model reads."""
    return chat_reads.scrub(text)


def _line_of(module, needle: str) -> int:
    lines = Path(module.__file__).read_text().splitlines()
    found = [i + 1 for i, line in enumerate(lines) if needle in line]
    assert len(found) == 1, (needle, found)
    return found[0]


@contextmanager
def display_at(module, needle: str, owner, name: str):
    """Mutation: the call to `owner.name` on the one source line of `module`
    holding `needle` gets the display scrub; every other call is unchanged."""
    real = getattr(owner, name)
    line, path = _line_of(module, needle), module.__file__

    def mutant(text, *a, **kw):
        frame = sys._getframe(1)
        if frame.f_code.co_filename == path and frame.f_lineno == line:
            return _display(text)
        return real(text, *a, **kw)
    with patch.object(owner, name, mutant):
        yield


class Relays(CR.Harness):
    def body_seen(self) -> str:
        sender, target = self.chat(), self.chat(CR.CLAUDE)
        self.turn(sender)
        self.sent(self.send(sender, target, KEEP))
        text = self.until_relayed(target).text
        self.release(sender)
        self.idle(target)
        self.idle(sender)
        return text

    def reply_seen(self, answer) -> str:
        sender, target = self.chat(), self.chat(CR.CLAUDE)
        self.fake(target).reply[target.id] = answer
        self.fake(target).hold.add(target.id)
        self.turn(sender)
        self.sent(self.send(sender, target, "tell me later"))
        self.until_relayed(target)
        self.release(sender)
        self.idle(sender)
        self.release(target)
        CR.until(lambda: self.reply_turns(sender), "the reply turn")
        self.idle(sender)
        return self.reply_turns(sender)[0].text

    def failure_seen(self) -> str:
        """A failed relay reaches a still-running sender's model as a step
        (to an idle sender it is only shown: P-10)."""
        sender, target = self.chat(), self.chat(CR.CLAUDE)
        self.fake(target).reply[target.id] = [Event(K.ERROR, target.id, {"message": REASON, "fatal": False}),
                                              Event(K.TURN_FINISHED, target.id, {"stop": "error"})]
        self.turn(sender)
        self.sent(self.send(sender, target, "q"))
        CR.until(lambda: self.reply_consumed(sender), "the failed reply at the next step")
        text = self.reply_consumed(sender)[0].text
        self.release(sender)
        self.idle(sender)
        self.idle(target)
        return text

    def test_m1_a_relay_body_reaches_the_model_unchanged(self):
        self.assertIn(KEEP, self.body_seen())
        print("ok  m1: a relay's body reaches the receiving model with every keep line as written")

    def test_m2_a_relay_reply_reaches_the_model_unchanged(self):
        self.assertIn(KEEP, self.reply_seen(KEEP))
        print("ok  m2: a relay's reply reaches the sending model with every keep line as written")

    def test_m3_a_relay_failure_reason_reaches_the_model_unchanged(self):
        self.assertIn(REASON, self.failure_seen())
        print("ok  m3: a relay's failure reason reaches the sending model as written")

    def test_m6_each_relay_call_site_on_the_display_scrub_is_caught(self):
        from jarvis.v2 import chat_relay
        cases = (("body = chat_reads.scrub_for_model(text)", self.body_seen, KEEP),
                 ('chat_reads.scrub_for_model("\\n\\n".join(texts))', lambda: self.reply_seen(KEEP), KEEP),
                 ("error or \"an error\"", self.failure_seen, REASON))
        for needle, seen, want in cases:
            with self.subTest(site=needle), display_at(chat_relay, needle, chat_reads, "scrub_for_model"):
                self.assertNotIn(want, seen(), f"mutation not caught at {needle}")
        print("ok  m6: each relay call site (body, reply, failure reason) put on the display scrub is caught")


class Direct(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(secrets, "secret_values", return_value=[]),
                        patch.object(secrets, "_process_env_values", return_value=())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_m4_chat_git_output_reaches_the_model_unchanged(self):
        self.assertIn(KEEP, chat_git._render(0, KEEP, ""))
        print("ok  m4: a chat git tool's output reaches the model with every keep line as written")

    def m5_failures(self) -> list[str]:
        from jarvis.v2.approvals import clean_line
        out = []
        if daemon.compact_reason(REASON) != clean_line(chat_reads.scrub(REASON), daemon.COMPACT_REASON_CHARS):
            out.append("not the owner's scrub")
        if "settings.API_KEY" in daemon.compact_reason(REASON):
            out.append("redact_inline not applied")
        return out

    def test_m5_a_compaction_reason_is_the_owners_scrub(self):
        self.assertEqual(self.m5_failures(), [])
        print("ok  m5: a compaction reason is shown and kept through the owner's scrub (redact_inline included)")

    def test_m6_chat_git_and_compaction_mutations_are_caught(self):
        with display_at(chat_git, "return for_model(secrets.strip_url_credentials(text))", redact, "for_model"):
            self.assertNotIn(KEEP, chat_git._render(0, KEEP, ""), "chat git mutation not caught")
        real = chat_reads.scrub_for_model
        with patch.object(chat_reads, "scrub", real):
            self.assertNotEqual(self.m5_failures(), [], "compaction mutation not caught")
        print("ok  m6: chat git output put on the display scrub, and the compaction reason on the model's, are caught")


if __name__ == "__main__":
    unittest.main(verbosity=1)
