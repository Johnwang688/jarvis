"""The offline pair check: the runtime dictation guard, called as is.

`jarvis.v2.dictation_guard.check` decides what the dictation path may type
(VA-3a). Training pairs are held to the same rule, so a model cannot learn an
edit the guard would refuse at runtime.
"""
from jarvis.v2.dictation_guard import check


def pair_check(messy: str, clean: str) -> tuple[bool, str]:
    verdict = check(messy, clean)
    return verdict.ok, verdict.reason
