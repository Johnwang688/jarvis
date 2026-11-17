"""In-memory mutants: production files are never edited; fake catalog tests only."""
from contextlib import redirect_stderr
import inspect
import io
from pathlib import Path
import runpy
import unittest
from types import FunctionType
from unittest.mock import patch

from jarvis.tools import files
from jarvis.v2 import claude_catalog as catalog
from jarvis.v2 import router


def main():
    checks = runpy.run_path(str(Path(__file__).with_name("claude_catalog_check.py")))
    parse_source = inspect.getsource(catalog)
    protection_source = inspect.getsource(files._protected_state_paths)
    sources = {
        "catalog": (parse_source, catalog, "parse", catalog.__dict__),
        "protection": (protection_source, files, "_protected_state_paths", files.__dict__),
        "offer": (inspect.getsource(router.offer_claude_rows), router, "offer_claude_rows", router.__dict__),
        "install": (inspect.getsource(router._install_claude_table), router, "_install_claude_table", router.__dict__),
    }
    mutants = [
        ("default row accepted", "catalog", ' or row.get("value") == "default"', "", "Parse", "test_sdk_rows_default_skip_resolved_id_first_wins_and_no_alias"),
        ("value chosen instead of resolved id", "catalog", 'model_id = (row.get("resolvedModel")', 'model_id = (row.get("value")', "Parse", "test_sdk_rows_default_skip_resolved_id_first_wins_and_no_alias"),
        ("invalid resolved id falls back to value", "catalog", 'isinstance(row.get("resolvedModel"), str)', 'isinstance(row.get("resolvedModel"), str) and _ID.fullmatch(row["resolvedModel"])', "Parse", "test_id_fallback_only_for_nonstring_resolved"),
        ("slug guard removed", "catalog", ' or not _ID.fullmatch(model_id)', "", "Parse", "test_slug_prefix_and_type_refusals"),
        ("Claude prefix guard removed", "catalog", 'not model_id.startswith("claude-")', 'False', "Parse", "test_slug_prefix_and_type_refusals"),
        ("last duplicate wins", "catalog", ' or model_id in parsed', "", "Parse", "test_sdk_rows_default_skip_resolved_id_first_wins_and_no_alias"),
        ("display name prefix omitted", "catalog", 'name = "Claude " + name', 'name = name', "Parse", "test_names_cleaned_prefixed_capped_and_fallback"),
        ("display name cleaning removed", "catalog", 'clean_line(display[:MAX_NAME * 4], MAX_NAME)', 'display', "Parse", "test_names_cleaned_prefixed_capped_and_fallback"),
        ("name cap removed", "catalog", 'name[:MAX_NAME] or model_id', 'name or model_id', "Parse", "test_names_cleaned_prefixed_capped_and_fallback"),
        ("efforts accepted without supportsEffort", "catalog", 'row.get("supportsEffort") is True and ', '', "Parse", "test_effort_requires_true_filters_words_and_preserves_order"),
        ("truthy supportsEffort accepted", "catalog", 'row.get("supportsEffort") is True', 'bool(row.get("supportsEffort"))', "Parse", "test_effort_requires_true_filters_words_and_preserves_order"),
        ("unknown effort accepted", "catalog", ' and effort in _CLAUDE_EFFORTS', '', "Parse", "test_effort_requires_true_filters_words_and_preserves_order"),
        ("duplicate effort accepted", "catalog", ' and effort not in efforts', '', "Parse", "test_effort_requires_true_filters_words_and_preserves_order"),
        ("effort cap removed", "catalog", 'advertised[:MAX_EFFORTS]', 'advertised', "Parse", "test_effort_requires_true_filters_words_and_preserves_order"),
        ("row cap removed", "catalog", 'rows[:MAX_ROWS]', 'rows', "Parse", "test_empty_garbage_and_row_model_caps"),
        ("model cap removed", "catalog", 'len(parsed) >= MAX_MODELS', 'False', "Parse", "test_empty_garbage_and_row_model_caps"),
        ("empty catalog accepted", "catalog", 'if not parsed:', 'if False:', "Parse", "test_empty_garbage_and_row_model_caps"),
        ("dated ids aliased", "catalog", 'efforts = []', 'model_id = model_id.removesuffix("-20251001")\n        efforts = []', "Parse", "test_sdk_rows_default_skip_resolved_id_first_wins_and_no_alias"),
        ("protected path omitted", "protection", 'config.CLAUDE_CATALOG_PATH.expanduser(),', '', "Protection", "test_protected_path_parity_direct_symlink_and_shell"),
        ("protected path missing from cache key", "protection", ' str(config.CLAUDE_CATALOG_PATH),', '', "Protection", "test_protected_path_parity_direct_symlink_and_shell"),
        ("validation after touching pending slot", "offer", 'parsed = claude_catalog.parse(rows)',
         'with _CLAUDE_QUEUE_LOCK:\n            _claude_pending = None\n        parsed = claude_catalog.parse(rows)',
         "Worker", "test_blocked_save_offers_connects_validation_and_lock"),
        ("save under config lock", "install", '    claude_catalog.save(parsed)',
         '    with _CONFIG_LOCK:\n        claude_catalog.save(parsed)',
         "Worker", "test_blocked_save_offers_connects_validation_and_lock"),
        ("ended generation not checked before save", "install",
         '    with _CLAUDE_QUEUE_LOCK:\n        if generation != _claude_generation:\n            return', '',
         "Worker", "test_generation_checked_again_before_save"),
        ("notify before swap", "install", '            CLI_MODELS["claude"] = parsed',
         '            pass', "Worker", "test_notification_observes_swap_and_equal_has_no_event"),

    ]
    killed = 0
    for name, module, old, new, cls, test in mutants:
        original, target, attr, globals_ = sources[module]
        assert original.count(old) == 1, (name, original.count(old))
        if name == "notify before swap":
            original += '\n    CLI_MODELS["claude"] = parsed\n'
        namespace = globals_.copy()
        exec(compile(original.replace(old, new), f"<mutant: {name}>", "exec"), namespace)
        if module in ("offer", "install"):
            namespace[attr] = FunctionType(namespace[attr].__code__, globals_, attr)
        files._STATE_PATHS.clear()
        with patch.object(target, attr, namespace[attr]), redirect_stderr(io.StringIO()):
            result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([checks[cls](test)]))
        files._STATE_PATHS.clear()
        # A meaningful assertion failure is required; a broken mutant raising
        # an unrelated error does not count as protection evidence.
        dead = bool(result.failures) and not result.errors
        print(f'{"KILLED" if dead else "SURVIVED"}: {name} -> {cls}.{test} ({len(result.failures)} assertion failures, {len(result.errors)} errors)')
        killed += int(dead)
    print(f"Mutation checks: {killed}/{len(mutants)} killed by assertions")
    return 0 if killed == len(mutants) else 1


if __name__ == "__main__":
    raise SystemExit(main())
