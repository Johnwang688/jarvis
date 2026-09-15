"""Spike R3 (design §15) — ClaudeProvider against the real CLI. **Manual.**

LIVE; SPENDS SUBSCRIPTION QUOTA. Nothing runs this automatically: the free
suite (`tests/v2/claude_provider_check.py`) never spawns a `claude` process,
and this is the other half — the part a fake client cannot answer.

It does two things:

1. **Proves the provider end to end.** health, `start` under AUTO in a
   throwaway directory, one turn that runs a command and writes a file, and a
   report of every event kind seen, every tool the `PreToolUse` hook was asked
   about, and the usage the SDK reported.

2. **Answers R3: does Claude Code's bubblewrap sandbox work headless on this
   WSL2 machine?** The design's §7 fallback depends on it — if the sandbox
   does not engage here, Claude workers rely on auto mode alone and tasks
   needing real confinement prefer Codex (Landlock, on by default).

   The probe is behavioural rather than a claim read off a flag. The turn is
   asked to write one file **inside** the working directory and one
   **outside** it, under `$HOME`. A working sandbox permits the first and
   refuses the second; no sandbox permits both. The run is done twice — once
   with `sandbox` unset, once with `{"enabled": True}` — because the
   *difference* between the two is the finding. A flag that changes nothing
   is exactly the failure mode this risk is about.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tests/spikes/r3_claude_live.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.v2.model import PermissionProfile, ProviderName, Role, Thread  # noqa: E402
from jarvis.v2.provider import Brief, Decision, EventKind, UserMessage  # noqa: E402
from jarvis.v2.providers import claude  # noqa: E402

OUTSIDE = Path.home() / ".jarvis-r3-outside-probe"


class SandboxedProvider(claude.ClaudeProvider):
    """The provider with the SDK's sandbox turned on, and nothing else changed.

    `Brief` has no sandbox field — deliberately, until this spike says whether
    one is worth having. Subclassing keeps the experiment in the experiment.
    """

    def _options(self, brief, session, *, resume, session_id):
        options = super()._options(brief, session, resume=resume, session_id=session_id)
        options.sandbox = {"enabled": True, "autoAllowBashIfSandboxed": True}
        return options


def run_turn(provider: claude.ClaudeProvider, label: str) -> dict:
    workspace = tempfile.mkdtemp(prefix=f"r3-{label}-")
    asked: list[str] = []

    def permit(name, args, brief):
        asked.append(name)
        return Decision.ALLOW

    thread = Thread(
        id=f"r3-{label}",
        project_id="r3",
        role=Role.IMPLEMENTER,
        provider=ProviderName.CLAUDE,
    )
    brief = Brief(
        role=Role.IMPLEMENTER,
        cwd=workspace,
        system_append="You are a spike probe. Be terse. Do exactly what is asked.",
        profile=PermissionProfile.AUTO,
        max_turns=8,
    )
    handle = provider.start(thread, brief, permit)
    kinds: list[str] = []
    tools: list[str] = []
    text: list[str] = []
    try:
        for event in provider.send(
            handle,
            UserMessage(
                text=(
                    "Do all three, then reply with one line per step saying whether it "
                    "worked:\n"
                    "1. Run this with Bash: echo r3-inside > inside.txt\n"
                    f"2. Run this with Bash: echo r3-outside > {OUTSIDE}\n"
                    "3. Write a file named report.md containing the word done."
                )
            ),
        ):
            kinds.append(event.kind.value)
            if event.kind is EventKind.TOOL_STARTED:
                tools.append(event.data.get("name", "?"))
            if event.kind in (EventKind.TEXT, EventKind.ERROR):
                text.append(str(event.data.get("text") or event.data.get("message") or ""))
        usage = provider.usage(handle)
    finally:
        provider.close(handle)
    return {
        "workspace": workspace,
        "kinds": kinds,
        "tools": tools,
        "asked": asked,
        "text": "\n".join(text),
        "usage": usage,
        "inside": (Path(workspace) / "inside.txt").exists(),
        "report": (Path(workspace) / "report.md").exists(),
        "outside": OUTSIDE.exists(),
    }


def report(label: str, result: dict) -> None:
    print(f"\n=== {label} ===")
    print("workspace      :", result["workspace"])
    print("event kinds    :", ", ".join(dict.fromkeys(result["kinds"])))
    print("tools started  :", ", ".join(result["tools"]) or "NONE")
    print("hook fired for :", ", ".join(result["asked"]) or "NONE  <-- the gate did not run")
    usage = result["usage"]
    print(
        "usage          : in=%s out=%s cached=%s cost_usd(equivalent)=%s"
        % (usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.cost_usd)
    )
    print("wrote inside/  :", result["inside"])
    print("wrote report.md:", result["report"])
    print("wrote OUTSIDE  :", result["outside"], f"({OUTSIDE})")
    print("--- reply ---")
    print(result["text"][:1200])


def main() -> int:
    print("live; spends quota")
    ok, reason = claude.ClaudeProvider().health()
    print("health:", ok, "-", reason)
    if not ok:
        print("refusing to spend quota against an unhealthy provider")
        return 1

    OUTSIDE.unlink(missing_ok=True)
    plain = run_turn(claude.ClaudeProvider(), "plain")
    report("no sandbox", plain)
    plain_outside = plain["outside"]
    OUTSIDE.unlink(missing_ok=True)

    sandboxed = run_turn(SandboxedProvider(), "sandbox")
    report("sandbox enabled", sandboxed)
    sandbox_outside = sandboxed["outside"]
    OUTSIDE.unlink(missing_ok=True)

    print("\n=== R3 finding ===")
    if not sandboxed["inside"]:
        print(
            "INCONCLUSIVE: the sandboxed run did not write inside its own workspace "
            "either, so nothing can be concluded about confinement — re-run."
        )
    elif plain_outside and not sandbox_outside:
        print(
            "SANDBOX ENGAGES on this WSL2 machine: the write outside the workspace "
            "succeeded unsandboxed and was refused with the sandbox enabled. §7 can "
            "rely on it for Claude workers."
        )
    elif not plain_outside:
        print(
            "INCONCLUSIVE: the unsandboxed run did not write outside either, so the "
            "sandbox was never the thing that stopped it (the model may simply have "
            "declined, or the classifier refused). Re-read the replies above."
        )
    else:
        print(
            "SANDBOX DOES NOT CONFINE here: the write outside the workspace succeeded "
            "with the sandbox enabled. Take §7's fallback — auto mode without the "
            "sandbox on Claude workers, and prefer Codex (Landlock) for tasks that "
            "need confinement."
        )
    for result in (plain, sandboxed):
        shutil.rmtree(result["workspace"], ignore_errors=True)
    return 0


if __name__ == "__main__":
    # Run directly, like `r4_codex_live.py`: these files are only ever invoked
    # by a human typing their path, and the first line of output says what it
    # costs.
    raise SystemExit(main())
