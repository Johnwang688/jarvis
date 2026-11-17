"""In-memory speed mutants; only assertion failures count as kills."""
import importlib
import inspect
import io
import json
from pathlib import Path
import subprocess
import tempfile
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
         speed_check.Choices("test_turn_downgrade_preserves_saved_choice_and_describe")),
        ("speed write omits model precondition", daemon.Daemon, "set_thread_model", daemon,
         '(body.get("speed_model") != current_model or current_model is None\n'
         '                    or ("model" in body and\n'
         '                        (body["model"] or current_model) != current_model))',
         'False', speed_check.LockedWrites("test_speed_model_precondition_rejects_old_model_even_with_same_tier")),
        ("reconnect records current speed instead of applied snapshot", daemon.Daemon, "_apply_choice", daemon,
         'session.applied_record_speed = record_speed', 'session.applied_record_speed = session.thread.speed',
         speed_check.LockedWrites("test_reconnect_rollback_uses_the_snapshot_applied_to_provider")),
        ("speed effective with model None", thread_model, "effective_speed", thread_model,
         'thread.model is None or thread.model != thread.speed_model\n                         or ', '',
         speed_check.Choices("test_default_following_speed_is_never_effective_even_at_its_bound_model")),
        ("default write activates speed when pinned check is skipped", thread_model, "effective_speed", thread_model,
         'thread.model is None or thread.model != thread.speed_model\n                         or ', '',
         backend.Backend("test_default_route_cannot_reactivate_speed_for_another_model")),
        ("model write binds an unbound legacy tier", daemon.Daemon, "set_thread_model", daemon,
         'elif speed_model is not None and speed in', 'elif speed in',
         speed_check.LockedWrites("test_owner_model_write_does_not_bind_legacy_speed")),
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
    killed += hud_chip_mutant()
    total = len(mutants) + 1
    print(f"Speed mutation checks: {killed}/{total} killed by assertions")
    return 0 if killed == total else 1


def hud_chip_mutant():
    """Transform the chip in Vite memory; never rewrite the working tree."""
    hud = Path(__file__).resolve().parents[2] / "hud"
    old, new = "(!tiers.length && !lit)", "!tiers.length"
    assert (hud / "src/components/SpeedChip.tsx").read_text().count(old) == 1
    with tempfile.TemporaryDirectory(prefix="speed-mutant-") as root:
        root = Path(root)
        config, report = root / "mutant.config.mjs", root / "result.json"
        config.write_text(
            f"import original from {json.dumps(str(hud / 'vite.config.ts'))};\n"
            "export default {...original, plugins: [...original.plugins, {\n"
            "name: 'speed-chip-mutant', enforce: 'pre',\n"
            "transform(source, id) { if (id.endsWith('/src/components/SpeedChip.tsx')) {\n"
            f"return source.replace({json.dumps(old)}, {json.dumps(new)});\n"
            "} } }]};\n")
        run = subprocess.run(["npx", "--offline", "--no-install", "vitest", "run",
                              "src/components/SpeedChip.test.ts", "--config", str(config),
                              "--maxWorkers=1", "--reporter=json", "--outputFile", str(report)],
                             cwd=hud, capture_output=True, text=True, timeout=120)
        result = json.loads(report.read_text()) if report.exists() else {}
        failures = [test for suite in result.get("testResults", [])
                    for test in suite.get("assertionResults", []) if test.get("status") == "failed"]
        dead = (run.returncode == 1 and len(failures) == 1 and
                "keeps a dormant saved speed enabled" in failures[0]["fullName"] and
                any("AssertionError" in msg for msg in failures[0].get("failureMessages", [])) and
                result.get("numRuntimeErrorTestSuites", 0) == 0 and
                result.get("numPassedTests", 0) > 0)
        print(f"{'KILLED' if dead else 'SURVIVED'}: chip disabled with a saved speed ({len(failures)} assertion failures)")
        if not dead:
            print(run.stdout, run.stderr)
        return int(dead)


if __name__ == "__main__":
    raise SystemExit(main())
