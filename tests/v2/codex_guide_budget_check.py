"""Codex chats must be able to load the whole repo guide (PR #413 review)."""
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from jarvis.v2.model import Role
from jarvis.v2.provider import Brief
from jarvis.v2.providers import codex_config


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROJECT_DOC_MAX_BYTES = 32 * 1024


class CodexGuideBudget(unittest.TestCase):
    def test_codex_config_can_load_the_full_jarvis_guide(self):
        brief = Brief(Role.CHAT, str(ROOT))
        with patch.object(codex_config, "filesystem", return_value={}):
            settings = tomllib.loads(codex_config.config_text(brief))
        limit = settings.get("project_doc_max_bytes", DEFAULT_PROJECT_DOC_MAX_BYTES)
        self.assertGreaterEqual(limit, (ROOT / "AGENTS.md").stat().st_size)


if __name__ == "__main__":
    unittest.main()
