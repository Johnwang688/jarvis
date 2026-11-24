"""Session-bound learned skill tools for v3 callers."""

from ..skill_store import StoreError


class SkillTools:
    def __init__(self, store, *, session, actor="reviewer", tainted=False):
        self._store = store
        self._session = session
        self._actor = actor
        self._tainted = tainted
        self._views = {}

    def skill_view(self, name, path="SKILL.md"):
        """Read a skill or one of its reference or template files."""
        try:
            return self._store._view(name, path, self._views)
        except StoreError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: The skill file could not be read safely."

    def skill_manage(self, action, name, **kw):
        """Create, patch, write a file, remove a file, or archive a learned skill."""
        try:
            return self._store._manage(action, name, self._views, self._actor,
                                       self._session, self._tainted, kw)
        except StoreError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: The skill change could not be completed safely."
