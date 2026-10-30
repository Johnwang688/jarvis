"""PR #343 verifier: valid PR-reader imports must fail the owner-only audit.

Run with the shared venv, a temporary HOME, and PYTHONPATH=$PWD.
No product source is modified and no PR/GitHub read is executed.
"""
from pathlib import Path
import importlib.util
import inspect
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "pr343_archive_audit", Path(__file__).with_name("archive_check.py")
)
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class PrFactsImportsMustBeRejected(unittest.TestCase):
    def test_real_owner_only_audit_rejects_valid_reader_imports(self):
        from jarvis.v2 import pr_facts
        from jarvis.v2.tools import owner_post

        audit = archive.OwnerOnly("test_no_tool_reaches_the_inbox")
        audit.test_no_tool_reaches_the_inbox()  # The unmodified audit passes.
        getsource = inspect.getsource
        original = getsource(owner_post)
        imports = {
            "importlib_relative": (
                "import importlib\n"
                "reader = importlib.import_module('.pr_facts', 'jarvis.v2')\n"
            ),
            "dunder_import_relative": (
                "reader = __import__('pr_facts', globals(), locals(), ['read_pr'], 2)\n"
            ),
            "parenthesized_relative": (
                "from .. import (pr_facts as reader)\n"
            ),
        }
        for spelling, source in imports.items():
            with self.subTest(spelling=spelling):
                namespace = {"__package__": "jarvis.v2.tools"}
                exec(compile(source, "<synthetic tool source>", "exec"), namespace)
                self.assertIs(namespace["reader"].read_pr, pr_facts.read_pr)
                injected = original + "\n" + source
                # Exercise the actual audit's complete source collection and
                # needles, injecting only what it sees for a reachable tool.
                def read_source(module):
                    return injected if module is owner_post else getsource(module)

                with patch.object(inspect, "getsource", side_effect=read_source):
                    # The pre-PR bare-name guard catches this same source.
                    with patch.object(archive.OwnerOnly, "INBOX_SOURCE",
                                      archive.OwnerOnly.INBOX_SOURCE + ("pr_facts",)):
                        with self.assertRaises(AssertionError):
                            audit.test_no_tool_reaches_the_inbox()
                    with self.assertRaises(AssertionError,
                                           msg=f"owner-only audit accepted {spelling}"):
                        audit.test_no_tool_reaches_the_inbox()


if __name__ == "__main__":
    unittest.main(verbosity=2)
