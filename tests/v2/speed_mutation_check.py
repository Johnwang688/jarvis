"""In-memory speed mutants; only assertion failures count as kills."""
import importlib
import inspect
import io
import textwrap
import unittest
from unittest.mock import patch

from jarvis.v2 import daemon, thread_model
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
        ("non-owner speed PATCH allowed", daemon.Daemon, "set_thread_model", daemon,
         'if "speed" in body or ("model" in body and thread.speed is not None):',
         'if False:', backend.Backend("test_speed_owner_write_validation_reset_and_model_change")),
        ("effort write restores a stale pre-lock speed", daemon.Daemon, "set_thread_model", daemon,
         ('    with self._lock:', '            speed, speed_model = thread.speed, thread.speed_model'),
         ('    stale = self.require(self.stores.threads, thread_id)\n    with self._lock:',
          '            speed, speed_model = thread.speed, thread.speed_model\n'
          '            if "speed" not in body and "model" not in body:\n'
          '                thread.speed, thread.speed_model = stale.speed, stale.speed_model'),
         speed_check.LockedWrites("test_non_owner_effort_cannot_restore_owner_speed_reset")),
        ("model authorization uses stale pre-lock speed", daemon.Daemon, "set_thread_model", daemon,
         ('    with self._lock:', '"model" in body and thread.speed is not None'),
         ('    stale = self.require(self.stores.threads, thread_id)\n    with self._lock:',
          '"model" in body and stale.speed is not None'),
         speed_check.LockedWrites("test_model_authorization_uses_locked_record")),
        ("default change reactivates a tier chosen for another model", thread_model, "effective_speed", thread_model,
         'thread.speed_model != model or thread.speed_model is None\n                         or ', '',
         speed_check.LockedWrites("test_default_change_cannot_reactivate_dormant_tier_and_owner_reselects")),
        ("speed write omits model precondition", daemon.Daemon, "set_thread_model", daemon,
         '(body.get("speed_model") != current_model or current_model is None\n'
         '                    or ("model" in body and\n'
         '                        (body["model"] or thread_model.default_model(thread.provider)) != current_model))',
         'False', speed_check.LockedWrites("test_speed_model_precondition_rejects_old_model_even_with_same_tier")),
        ("reconnect records current speed instead of applied snapshot", daemon.Daemon, "_apply_choice", daemon,
         'session.applied_record_speed = record_speed', 'session.applied_record_speed = session.thread.speed',
         speed_check.LockedWrites("test_reconnect_rollback_uses_the_snapshot_applied_to_provider")),
    ]
    killed = 0
    for name, target, attr, module, old, new, test in mutants:
        source = textwrap.dedent(inspect.getsource(getattr(target, attr)))
        replacements = zip(old, new) if isinstance(old, tuple) else [(old, new)]
        for before, after in replacements:
            # Reconnect's snapshot assignment appears in both the no-op and
            # successful-switch branches; mutate both deliberately.
            expected = 2 if name.startswith("reconnect records") else 1
            assert source.count(before) == expected, (name, before, source.count(before))
            source = source.replace(before, after)
        namespace = module.__dict__.copy()
        exec(compile(source, f"<mutant: {name}>", "exec"), namespace)
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
