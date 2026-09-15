"""Manual R4 smoke: the owner runs this explicitly; never imported by free tests.

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python tests/spikes/r4_codex_live.py
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import uuid

from jarvis import config
from jarvis.v2.model import ProviderName, Role, Thread
from jarvis.v2.provider import Brief, Decision, EventKind, UserMessage
from jarvis.v2.providers.codex import CodexProvider


def main():
    parser = argparse.ArgumentParser(description="live; spends quota")
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", default=None)
    args = parser.parse_args()
    print("live; spends quota", flush=True)
    provider = CodexProvider()
    ok, reason = provider.health()
    print("health:", ok, reason)
    if not ok:
        return 1

    def permit(tool, arguments, brief):
        print("approval:", tool, json.dumps(arguments, indent=2), flush=True)
        try:
            return Decision.ALLOW if input("Allow this request? [yes/NO] ").strip().lower() == "yes" else Decision.DENY
        except (EOFError, KeyboardInterrupt):
            return Decision.DENY

    # Keep all smoke state and files temporary; the owner login is linked only.
    with tempfile.TemporaryDirectory(prefix="jarvis-r4-live-") as temporary:
        root = Path(temporary)
        work = root / "work"
        work.mkdir()
        previous = config.V2_DATA_DIR
        config.V2_DATA_DIR = root / "state"
        h = None
        try:
            thread = Thread("smoke-" + uuid.uuid4().hex, "r4", Role.IMPLEMENTER, ProviderName.CODEX)
            brief = Brief(Role.IMPLEMENTER, str(work), model=args.model, effort=args.effort,
                          system_append="This is a one-turn smoke test. Only perform the requested local checks.")
            h = provider.start(thread, brief, permit)
            kinds = []
            for event in provider.send(h, UserMessage(
                "Run `echo ok` using your shell tool, and use your file editing tool to create "
                "smoke-ok.txt containing exactly `ok` and a newline in this working directory. "
                "Then give a short final answer. Do not access any external service.")):
                kinds.append(event.kind)
                print(event.kind.value, flush=True)
                if event.kind == EventKind.QUESTION:
                    try:
                        answer = input(event.data["text"] + " ")
                    except EOFError:
                        answer = "Stop the smoke test."
                    provider.answer(h, event.data["req_id"], answer)
                elif event.kind == EventKind.ERROR:
                    print(event.data)
            print("event kinds:", [k.value for k in kinds])
            print("approval arrived:", EventKind.APPROVAL_REQUESTED in kinds)
            print("usage:", asdict(provider.usage(h)))
            target = work / "smoke-ok.txt"
            written = target.is_file() and target.read_text() == "ok\n"
            print("file verified:", written)
            return 0 if written and EventKind.TURN_FINISHED in kinds and EventKind.ERROR not in kinds else 1
        finally:
            if h is not None:
                provider.close(h)
            config.V2_DATA_DIR = previous


if __name__ == "__main__":
    raise SystemExit(main())
