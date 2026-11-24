"""Session-bound learned skill tools for v3 callers."""

from typing import Annotated, TypedDict, Unpack


class _ChangeFields(TypedDict, total=False):
    description: Annotated[str, "One-line description for create."]
    body: Annotated[str, "Markdown skill body for create."]
    path: Annotated[str, "Relative skill file path for patch, write_file, or remove_file."]
    old: Annotated[str, "Unique text to replace when patching."]
    new: Annotated[str, "Replacement text for patch."]
    content: Annotated[str, "File text for write_file."]


class SkillTools:
    def __init__(self, store, *, session, actor="reviewer", tainted=False):
        self._store = store
        self._session = session
        self._actor = actor
        self._tainted = tainted
        self._views = {}

    def skill_view(
        self,
        name: Annotated[str, "Name of the skill to read."],
        path: Annotated[str, "Relative path inside the skill."] = "SKILL.md",
    ):
        """Read a skill or one of its reference or template files."""
        from ..skill_store import StoreError

        try:
            return self._store._view(name, path, self._views)
        except StoreError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: The skill file could not be read safely."

    def skill_manage(
        self,
        action: Annotated[str, "Choose create, patch, write_file, remove_file, or archive."],
        name: Annotated[str, "Name of the learned skill."],
        **kw: Unpack[_ChangeFields],
    ):
        """Create, patch, write a file, remove a file, or archive a learned skill."""
        from ..skill_store import StoreError

        try:
            return self._store._manage(action, name, self._views, self._actor,
                                       self._session, self._tainted, kw)
        except StoreError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: The skill change could not be completed safely."
