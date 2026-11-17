"""In-memory speed mutants; only assertion failures count as kills."""
import importlib
import inspect
import io
import textwrap
import unittest
from unittest.mock import patch

from jarvis.v2 import hud_api, thread_model
from jarvis.v2.providers import claude, codex
from tests.v2 import speed_check


def main():
    backend = importlib.import_module("tests.v2.hud_backend_check")
    codex_checks = importlib.import_module("tests.v2.codex_provider_check")
    mutants = [
        ("Claude fastMode enabled at Standard", claude.ClaudeProvider, "_options", claude,
         'brief.speed == "fast" and', 'True and',
         speed_check.Choices("test_claude_fastmode_only_when_fast_and_supported")),
        ("Claude ignores model fast capability", claude.ClaudeProvider, "_options", claude,
         'any(t[0] == "fast" for t in speeds_of("claude", brief.model))', 'True',
         speed_check.Choices("test_claude_fastmode_only_when_fast_and_supported")),
        ("Codex omits explicit default reset", codex.CodexProvider, "_drive", codex,
         's.brief.speed or "default"', 's.brief.speed',
         codex_checks.Checks("test_speed_is_a_per_turn_override_and_standard_resets")),
        ("speed accepted outside current model catalog", thread_model, "check_speed", thread_model,
         'speed not in {tier[0] for tier in speeds_of(provider, target)}', 'False',
         speed_check.Choices("test_speed_validation_current_model_and_reset")),
        ("non-owner speed PATCH allowed", hud_api, "route", hud_api,
         'if "speed" in body or ("model" in body and daemon.require(stores.threads, parts[1]).speed is not None):',
         'if False:', backend.Backend("test_speed_owner_write_validation_reset_and_model_change")),
    ]
    killed = 0
    for name, target, attr, module, old, new, test in mutants:
        source = textwrap.dedent(inspect.getsource(getattr(target, attr)))
        assert source.count(old) == 1, name
        namespace = module.__dict__.copy()
        exec(compile(source.replace(old, new), f"<mutant: {name}>", "exec"), namespace)
        with patch.object(target, attr, namespace[attr]):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([test]))
        dead = bool(result.failures) and not result.errors
        print(f"{'KILLED' if dead else 'SURVIVED'}: {name} ({len(result.failures)} assertion failures, {len(result.errors)} errors)")
        if not dead:
            print(result.errors or result.failures)
        killed += dead
    print(f"Speed mutation checks: {killed}/{len(mutants)} killed by assertions")
    return 0 if killed == len(mutants) else 1


if __name__ == "__main__":
    raise SystemExit(main())
