"""Tree rendering and ref resolution — the OS-independent half of the bridge.

Split out of `bridge.py` so it can be tested on Linux for free. Everything
here talks to a *backend* duck-type with two methods:

    backend.children(node) -> list[node]
    backend.describe(node) -> dict with role/name/value/checked/offscreen/
                              enabled/interactive/rect

`bridge.py` supplies the two real ones (UI Automation and MSAA); the tests
supply a dict-tree fake. No Windows imports belong in this file.
"""

from __future__ import annotations

# The one window the bridge refuses to touch, whatever the server asks for.
#
# The face HUD carries the approval card for dangerous tools, so an agent that
# could drive that window could click its own AUTHORIZE button — the same hole
# `config.is_face_origin()` closes for the browser. The app registry already
# makes this unreachable (no browser is registered); this is the backstop for
# the day someone registers one.
FORBIDDEN_TITLES = ("j.a.r.v.i.s.", "jarvis — design board", "jarvis - design board")

# Containers that carry no meaning of their own. Skipped when they are unnamed
# and non-interactive, so the tree reads as UI rather than as scaffolding.
STRUCTURAL_ROLES = frozenset({
    "pane", "custom", "group", "client", "document", "window", "table", "row",
    "column", "border", "whitespace", "separator", "scrollbar", "titlebar",
})


def forbidden_title(title: str) -> bool:
    return (title or "").strip().lower() in FORBIDDEN_TITLES


def clean(text: str | None) -> str:
    """Strip icon-font glyphs and collapse whitespace.

    Windows ships icon fonts (Segoe MDL2) whose glyphs live in the Unicode
    private use area. They arrive as accessible names — the Settings search
    button is literally named "" — and they are pure noise to a model as
    well as an encoding hazard on any non-UTF-8 stream.
    """
    if not text:
        return ""
    out = "".join(" " if 0xE000 <= ord(c) <= 0xF8FF else c for c in text)
    return " ".join(out.split())


def snapshot(backend, max_elements: int = 150) -> tuple[str, dict]:
    """Render the tree the way the browser tools render a page.

    The model gets `[e12] button "Save"` lines and passes e12 back. Refs beat
    coordinates for the same reason they do in the browser: no arithmetic, no
    guessing, and a stale ref fails loudly instead of clicking whatever moved
    into its place.
    """
    lines: list[str] = []
    refs: dict[str, dict] = {}
    counter = [0]
    shown = [0]

    def walk(node, path: list[int], depth: int, parent_name: str) -> None:
        if shown[0] >= max_elements or depth > 30:
            return
        for index, child in enumerate(backend.children(node)):
            if shown[0] >= max_elements:
                return
            info = backend.describe(child)
            child_path = path + [index]
            name, role = info["name"], info["role"]
            interactive = info["interactive"]

            # Collapse the label-inside-a-control duplication both backends
            # produce: a listitem named "Personalization" wrapping a text node
            # with the identical string carries no extra signal.
            redundant = bool(name) and name == parent_name and not interactive
            structural = (
                role in STRUCTURAL_ROLES and not interactive and not name
            )

            if not info["offscreen"] and not structural and not redundant and (
                name or interactive or info["value"]
            ):
                indent = "  " * min(depth, 8)
                if interactive:
                    counter[0] += 1
                    ref = f"e{counter[0]}"
                    refs[ref] = {"path": child_path, "role": role, "name": name}
                    tag = f"[{ref}] "
                else:
                    tag = "     "
                # A Windows 11 switch arrives as a *button* that happens to
                # carry a toggle state, and rendering that as
                # `button "Bluetooth" checked=true` was ambiguous enough that
                # the model read a radio that was on as off — "checked" on a
                # button reads like "pressed". Call a switch a switch and put
                # its state in words.
                shown_role = role
                state = ""
                if info["checked"] is not None:
                    if role in ("button", "menuitem", "splitbutton"):
                        shown_role = "switch"
                        state = " ON" if info["checked"] else " OFF"
                    else:
                        state = f' checked={str(info["checked"]).lower()}'

                bits = f"{tag}{indent}{shown_role}"
                if name:
                    bits += f' "{name}"'
                if info["value"]:
                    bits += f' value="{info["value"]}"'
                bits += state
                # Selection is only worth a word when it is true: a nav list
                # would otherwise carry a dozen "selected=false" tails.
                if info.get("selected"):
                    bits += " selected"
                if not info["enabled"]:
                    bits += " disabled"
                lines.append(bits)
                shown[0] += 1
                deeper = depth + 1
            else:
                deeper = depth

            if not info["offscreen"]:
                walk(child, child_path, deeper, name or parent_name)

    walk(backend.root, [], 0, "")
    text = "\n".join(lines) if lines else (
        "(no accessible elements — the window may still be loading)")
    if shown[0] >= max_elements:
        text += f"\n… truncated at {max_elements} elements."
    return text, refs


class StaleRef(RuntimeError):
    """A ref no longer points at the element the snapshot recorded."""


def resolve(backend, ref_entry: dict):
    """Turn a stored ref back into a live element.

    The recorded path is tried first, then *verified* against the role and
    name the snapshot saw. UI trees shift under you — a list re-sorts, a
    spinner disappears, a notification pushes everything down one slot — so a
    path that now lands on something else must never be clicked. On mismatch,
    fall back to a unique (role, name) search; if that is ambiguous or absent,
    fail loudly and make the caller re-snapshot.
    """
    node = backend.root
    for index in ref_entry["path"]:
        kids = backend.children(node)
        if index >= len(kids):
            node = None
            break
        node = kids[index]

    if node is not None:
        info = backend.describe(node)
        if info["role"] == ref_entry["role"] and info["name"] == ref_entry["name"]:
            return node

    matches: list = []

    def search(n, depth=0):
        if depth > 30 or len(matches) > 1:
            return
        for c in backend.children(n):
            info = backend.describe(c)
            if info["role"] == ref_entry["role"] and info["name"] == ref_entry["name"]:
                matches.append(c)
                if len(matches) > 1:
                    return
            search(c, depth + 1)

    search(backend.root)
    if len(matches) == 1:
        return matches[0]
    raise StaleRef(
        f"stale ref: {ref_entry['role']} {ref_entry['name']!r} is no longer at "
        "that position in the tree. Take a fresh desktop_snapshot."
    )
