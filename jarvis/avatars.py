"""Avatars — who Jarvis is presenting as: name, wake phrases, and a face.

An avatar is a directory under `config.AVATARS_DIR` (gitignored — avatars are
the owner's, not the project's):

    avatars/<slug>/avatar.svg     the face, drawn in the orb
    avatars/<slug>/avatar.json    {name, wake, accent, banner, label, ...}

Three things change when one is active: the **name** the model answers to (an
identity rename applied to the system prompt at every step, so it survives
compaction), the **wake phrases** the HUD listens for, and the **art** in the
centre of the orb.

Nothing here is a security boundary being loosened, but one thing here *is* a
boundary: an avatar SVG is drawn inside `jarvis.html`, the window that gates
approvals, and `write_file` can reach `avatars/`. So the SVG is treated as
untrusted input in exactly the way web content is:

  1. `sanitize_svg()` rebuilds the file from an element/attribute allowlist —
     no script, no event handlers, no external references, no entities.
  2. The face serves it as its own resource and the HUD renders it in an
     `<img>`, which cannot run script or load anything whatever the file says.

Layer 2 is the real protection; layer 1 is so a broken avatar fails as a
missing face rather than a silent surprise. Same shape as the secrets rules.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import config

SVG_NS = "http://www.w3.org/2000/svg"
MAX_SVG_BYTES = 512 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


# --------------------------------------------------------------------------
# the avatar itself
# --------------------------------------------------------------------------


@dataclass
class Avatar:
    slug: str
    name: str
    # Plain spoken phrases. Compiled to anchored regexes by wake_patterns();
    # an avatar author should never have to write a regex.
    wake: list[str] = field(default_factory=list)
    # Escape hatch for a phrase the recognizer mangles — raw JS-compatible
    # regex source. Anchoring is enforced, not assumed (see wake_patterns).
    wake_regex: list[str] = field(default_factory=list)
    # How he sounds. A Kokoro voice name (`voice.available_voices()` lists
    # what this machine can actually say); empty means config.TTS_VOICE.
    # Resolution and validation live in voice.py — an avatar naming a voice
    # that is not installed must leave him *audible*, the same rule that keeps
    # a broken wake regex from leaving him unsummonable.
    voice: str = ""
    # Speech-rate multiplier, 0 = "use config.TTS_SPEED".
    speed: float = 0.0
    # Which outer ring the orb turns ("triangles"; anything the HUD does not
    # know falls back to the default set, there as here).
    rings: str = ""
    accent: str = ""
    banner: str = ""
    label: str = ""
    description: str = ""
    svg_path: Path | None = None

    def banner_text(self) -> str:
        """The spaced title across the top of the HUD."""
        return self.banner or " ".join(self.name.upper())

    def label_text(self) -> str:
        """What the COMMS log calls him."""
        return self.label or self.name.upper()

    def wake_labels(self) -> list[str]:
        """What the armed hint says out loud. An undiscoverable wake word is
        one nobody says, so this is derived from the phrases, not free text."""
        return [p.upper() for p in self.wake] or [self.name.upper()]

    def describe(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "banner": self.banner_text(),
            "label": self.label_text(),
            "wake": self.wake_labels(),
            "wake_regex": wake_patterns(self),
            "accent": accent_rgb(self.accent),
            "rings": self.rings,
            "description": self.description,
            "has_svg": self.svg_path is not None,
        }


# The built-in. Not on disk, so a fresh clone with no `avatars/` behaves the
# same way — the triangle emblem and the wake phrase the HUD hard-codes. That
# regex is duplicated in `jarvis.html` as its no-config fallback;
# `tests/avatars_check.py` asserts the two copies stay identical.
#
# "big yahoo" used to live here too. It moved to the `bibi` avatar on
# 2026-08-06, where the pun belongs: a wake phrase is part of *who he is
# presenting as*, and the default should only answer to its own name.
DEFAULT = Avatar(
    slug="jarvis",
    name="Jarvis",
    wake=["jarvis"],
    wake_regex=[
        r"\bjarvis\b",
    ],
    banner="J A R V I S",
    label="J.A.R.V.I.S.",
    accent="#38bdf8",
    description="The default: cyan arc reactor, triangle emblem.",
)


# --------------------------------------------------------------------------
# wake phrases
# --------------------------------------------------------------------------


def phrase_regex(phrase: str) -> str:
    """Turn a spoken phrase into an anchored, spacing-tolerant regex.

    Two rules, both learned the hard way (see CLAUDE.md, wake phrases):

    * `\\b` at both ends. A wake hit cancels the turn in flight and starts
      recording, so an unanchored pattern eats the owner's words mid-sentence.
    * tolerate the recognizer's spacing. It is matching a *live interim*
      transcript of a phrase it has never heard, so "big yahoo" comes back as
      "big ya hoo" as often as not.
    """
    words = [re.escape(w) for w in str(phrase).lower().split() if w]
    if not words:
        return ""
    return r"\b" + r"\s*".join(words) + r"\b"


def wake_patterns(av: "Avatar") -> list[str]:
    """Every regex source this avatar wakes on, anchored and validated."""
    out: list[str] = []
    for raw in av.wake_regex:
        src = str(raw)
        try:
            re.compile(src)
        except re.error:
            continue  # a broken pattern is a silent avatar, not a crash
        if not src.startswith(r"\b"):
            src = r"\b" + src
        if not src.endswith(r"\b"):
            src = src + r"\b"
        out.append(src)
    for phrase in av.wake:
        src = phrase_regex(phrase)
        if src and src not in out:
            out.append(src)
    return out


def matches_wake(text: str, av: "Avatar | None" = None) -> bool:
    """Server-side twin of the HUD's matchesWake — used by the tests."""
    av = av or active()
    return any(re.search(p, text or "", re.I) for p in wake_patterns(av))


def accent_rgb(value: str) -> str:
    """`#7dd3fc` -> `125,211,252`, the form the orb's palette already speaks."""
    hexed = (value or "").strip().lstrip("#")
    if len(hexed) == 3:
        hexed = "".join(c * 2 for c in hexed)
    if len(hexed) != 6:
        return ""
    try:
        r, g, b = (int(hexed[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return ""
    return f"{r},{g},{b}"


# --------------------------------------------------------------------------
# SVG sanitizing
# --------------------------------------------------------------------------

ALLOWED_TAGS = frozenset({
    "svg", "g", "defs", "title", "desc", "path", "circle", "ellipse", "rect",
    "line", "polyline", "polygon", "text", "tspan", "linearGradient",
    "radialGradient", "stop", "clipPath", "mask", "pattern", "use", "symbol",
})

ALLOWED_ATTRS = frozenset({
    "id", "class", "d", "fill", "fill-opacity", "fill-rule", "clip-rule",
    "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "stroke-miterlimit",
    "opacity", "transform", "viewBox", "preserveAspectRatio", "version",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry",
    "width", "height", "points", "offset", "stop-color", "stop-opacity",
    "gradientUnits", "gradientTransform", "spreadMethod", "patternUnits",
    "patternContentUnits", "clip-path", "mask", "clipPathUnits", "maskUnits",
    "font-size", "font-family", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "letter-spacing", "vector-effect", "paint-order",
    "style", "href",
})

# Anything a value can carry that would reach outside the file.
_BAD_VALUE = re.compile(r"javascript:|data:text/html|expression\s*\(|@import|url\s*\(", re.I)


class SvgRejected(ValueError):
    """The file is not something we are willing to put in the HUD."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def sanitize_svg(source: str) -> str:
    """Rebuild an SVG from the allowlist, or raise SvgRejected.

    Allowlist, never denylist: the attack surface of SVG is large enough that
    enumerating what is dangerous is a losing game, and an avatar only needs
    geometry.
    """
    text = source if isinstance(source, str) else source.decode("utf-8", "replace")
    if len(text.encode("utf-8")) > MAX_SVG_BYTES:
        raise SvgRejected(f"svg larger than {MAX_SVG_BYTES // 1024}KB")
    # Entities are an expansion bomb and a file-read primitive; nothing an
    # avatar needs uses them, so refuse before the parser ever sees one.
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise SvgRejected("doctype/entity declarations are not allowed")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise SvgRejected(f"not parseable as XML: {exc}") from None
    if _local(root.tag) != "svg":
        raise SvgRejected(f"root element is <{_local(root.tag)}>, not <svg>")

    out = _clean(root)
    if out is None:
        raise SvgRejected("nothing left after sanitizing")

    # Size comes from the HUD's CSS; the file supplies shape only. Dropping
    # width/height leaves the viewBox in charge, so any avatar scales.
    view = out.get("viewBox") or _derived_viewbox(root)
    for attr in ("width", "height"):
        out.attrib.pop(attr, None)
    out.set("viewBox", view)
    out.set("xmlns", SVG_NS)

    ET.register_namespace("", SVG_NS)
    return ET.tostring(out, encoding="unicode")


def _derived_viewbox(root: ET.Element) -> str:
    def num(name: str) -> float:
        raw = re.sub(r"[^0-9.\-]", "", root.get(name, "") or "")
        try:
            return float(raw)
        except ValueError:
            return 0.0

    w, h = num("width"), num("height")
    return f"0 0 {w:g} {h:g}" if w > 0 and h > 0 else "0 0 100 100"


def _clean(node: ET.Element) -> ET.Element | None:
    tag = _local(node.tag)
    if tag not in ALLOWED_TAGS:
        return None
    out = ET.Element(tag)

    for raw_name, value in node.attrib.items():
        name = _local(raw_name)
        if name.lower().startswith("on"):
            continue  # onload/onclick/onmouseover — the whole point
        if name not in ALLOWED_ATTRS:
            continue
        if _BAD_VALUE.search(value or ""):
            continue
        if name == "href" and not (value or "").startswith("#"):
            continue  # local fragment refs only; never a network fetch
        out.set(name, value)
    out.text = node.text
    out.tail = node.tail
    for kept in _clean_children(node):
        out.append(kept)
    return out


# Wrappers that carry no geometry of their own. Editors emit them constantly
# (`<a>` around a shape, `<switch>` around a fallback), and dropping the whole
# subtree would silently lose the drawing — so they are unwrapped, not kept:
# the container and its dangerous attributes go, its allowed children stay.
TRANSPARENT_TAGS = frozenset({"a", "switch", "metadata"})


def _clean_children(node: ET.Element) -> list[ET.Element]:
    out: list[ET.Element] = []
    for child in list(node):
        tag = _local(child.tag)
        if tag in TRANSPARENT_TAGS:
            out.extend(_clean_children(child))
            continue
        kept = _clean(child)
        if kept is not None:
            out.append(kept)
    return out


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _number(value) -> float:
    """A hand-written avatar.json is untrusted input like the SVG is — a
    speed of "fast" is a fallback to the configured pace, not a crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load_dir(path: Path) -> Avatar | None:
    if not path.is_dir() or not SLUG_RE.match(path.name):
        return None
    meta = _read_json(path / "avatar.json")
    svg = path / "avatar.svg"
    name = str(meta.get("name") or path.name).strip() or path.name
    wake = [str(w) for w in meta.get("wake", []) if str(w).strip()]
    return Avatar(
        slug=path.name,
        name=name,
        # An avatar with no stated wake phrase still has to be summonable, or
        # switching to it silently disarms the wake word.
        wake=wake or [name.lower()],
        wake_regex=[str(w) for w in meta.get("wake_regex", []) if str(w).strip()],
        voice=str(meta.get("voice") or "").strip(),
        speed=_number(meta.get("speed")),
        rings=str(meta.get("rings") or "").strip(),
        accent=str(meta.get("accent") or ""),
        banner=str(meta.get("banner") or ""),
        label=str(meta.get("label") or ""),
        description=str(meta.get("description") or ""),
        svg_path=svg if svg.is_file() else None,
    )


def available() -> list[Avatar]:
    """Every avatar on disk, plus the built-in default, slug-sorted."""
    found: list[Avatar] = []
    root = config.AVATARS_DIR
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            av = _load_dir(entry)
            if av is not None and av.slug != DEFAULT.slug:
                found.append(av)
    return [DEFAULT] + found


def load(slug: str) -> Avatar | None:
    slug = (slug or "").strip().lower()
    if slug in ("", DEFAULT.slug):
        return DEFAULT
    if not SLUG_RE.match(slug):
        return None
    return _load_dir(config.AVATARS_DIR / slug)


def active_slug() -> str:
    """Env wins, then the saved pointer, then the default."""
    env = (config.AVATAR_ENV or "").strip().lower()
    if env:
        return env
    return str(_read_json(config.AVATAR_STATE_PATH).get("slug") or DEFAULT.slug)


def active() -> Avatar:
    """Never raises and never returns None — a missing avatar is the default,
    because the HUD losing its wake word over a typo would be worse."""
    return load(active_slug()) or DEFAULT


def set_active(slug: str) -> Avatar:
    """Switch avatars. Raises LookupError if there is no such avatar."""
    av = load(slug)
    if av is None:
        raise LookupError(f"no avatar named {slug!r}")
    path = config.AVATAR_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"slug": av.slug}, indent=2) + "\n", encoding="utf-8")
    # An explicit switch wins over `jarvis face -a` / JARVIS_AVATAR. Without
    # this the pin would keep answering `active()`, so the picker would save
    # the new avatar, redraw the window, and serve the old one — a state where
    # the window and the server disagree about who he is.
    config.AVATAR_ENV = ""
    for callback in list(_listeners):
        try:
            callback(av)
        except Exception:
            pass
    return av


_listeners: list = []


def on_change(callback) -> None:
    """The face registers here so the window redraws when the avatar changes
    — same wiring as voicectl's mute broadcast."""
    _listeners.append(callback)


def svg(av: Avatar | None = None) -> str | None:
    """The active avatar's art, sanitized. None if it has none or it failed."""
    av = av or active()
    if av.svg_path is None:
        return None
    try:
        return sanitize_svg(av.svg_path.read_text(encoding="utf-8"))
    except (OSError, SvgRejected):
        return None


# --------------------------------------------------------------------------
# identity in the prompt
# --------------------------------------------------------------------------

_RENAME = re.compile(r"\bJarvis\b")


def rename(text: str, av: Avatar | None = None) -> str:
    """Rewrite the system prompt in the active avatar's name.

    Applied in `Agent._refresh_system`, which runs at the top of every *step*
    — so it reaches every surface (the HUD, Discord, goals, workflows all
    build their prompt from `config.SYSTEM_PROMPT`) and it survives
    compaction, which never touches messages[0].

    Case-sensitive on purpose: the prompt also names shell commands
    (`jarvis chat -c`, `jarvis face`) and those must not be renamed into
    commands that do not exist.
    """
    av = av or active()
    if av.name == DEFAULT.name:
        return text
    out = _RENAME.sub(av.name, text)
    also = ""
    if DEFAULT.name.lower() not in {w.lower() for w in av.wake}:
        also = (
            f' The owner may still slip and call you {DEFAULT.name}; answer to '
            "either, but use your own name."
        )
    return out + (
        f"\n\nYou are currently going by **{av.name}**, the avatar the owner "
        f"has selected. Refer to yourself that way in speech and in writing."
        + also
    )
