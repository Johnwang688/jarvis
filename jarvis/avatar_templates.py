"""Starter avatars for `jarvis avatar new`.

`avatars/` is gitignored, so a fresh clone has nothing to look at. These are
the scaffolding: line-art SVGs in the HUD's own idiom (stroked, no fill, a
100x100 viewBox) that the owner can open in the design board — or any editor —
and replace. They are deliberately simple; the point is a working avatar in
one command, not artwork.

Each is (description, accent, svg). Adding one is adding an entry.
"""

from __future__ import annotations

_FOX = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g fill="none" stroke="#7dd3fc" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M22 30 L18 12 L38 22"/>
    <path d="M78 30 L82 12 L62 22"/>
    <path d="M22 30 C22 20 34 18 50 18 C66 18 78 20 78 30
             C78 52 68 66 50 82 C32 66 22 52 22 30 Z"/>
    <path d="M50 82 L50 66"/>
    <path d="M40 62 L50 68 L60 62"/>
  </g>
  <g fill="#7dd3fc" stroke="none">
    <circle cx="37" cy="44" r="4"/>
    <circle cx="63" cy="44" r="4"/>
    <circle cx="50" cy="63" r="3.2"/>
  </g>
  <g fill="none" stroke="#38bdf8" stroke-width="1.6" opacity=".65"
     stroke-linecap="round">
    <path d="M28 52 L10 48"/>
    <path d="M28 58 L12 60"/>
    <path d="M72 52 L90 48"/>
    <path d="M72 58 L88 60"/>
  </g>
</svg>
"""

_OWL = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g fill="none" stroke="#7dd3fc" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M20 40 C20 22 33 12 50 12 C67 12 80 22 80 40
             C80 64 68 84 50 88 C32 84 20 64 20 40 Z"/>
    <path d="M22 32 L30 18 L42 26"/>
    <path d="M78 32 L70 18 L58 26"/>
    <circle cx="37" cy="42" r="11"/>
    <circle cx="63" cy="42" r="11"/>
    <path d="M50 50 L45 58 L50 62 L55 58 Z"/>
    <path d="M34 70 C40 76 60 76 66 70"/>
  </g>
  <g fill="#7dd3fc" stroke="none">
    <circle cx="37" cy="42" r="4.5"/>
    <circle cx="63" cy="42" r="4.5"/>
  </g>
</svg>
"""

_BUST = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g fill="none" stroke="#7dd3fc" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <circle cx="50" cy="34" r="19"/>
    <path d="M14 92 C14 72 30 60 50 60 C70 60 86 72 86 92"/>
    <path d="M31 24 C36 12 64 12 69 24"/>
  </g>
  <g fill="none" stroke="#38bdf8" stroke-width="1.4" opacity=".55">
    <circle cx="50" cy="34" r="27"/>
    <path d="M26 88 L74 88"/>
  </g>
  <g fill="#7dd3fc" stroke="none">
    <circle cx="43" cy="33" r="2.6"/>
    <circle cx="57" cy="33" r="2.6"/>
  </g>
</svg>
"""

_REACTOR = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g fill="none" stroke="#7dd3fc" stroke-width="2.4" stroke-linejoin="round">
    <circle cx="50" cy="50" r="30"/>
    <circle cx="50" cy="50" r="20"/>
    <path d="M50 26 L71 62 L29 62 Z"/>
  </g>
  <g fill="none" stroke="#38bdf8" stroke-width="1.5" opacity=".6">
    <path d="M50 8 L50 18"/><path d="M50 82 L50 92"/>
    <path d="M8 50 L18 50"/><path d="M82 50 L92 50"/>
    <circle cx="50" cy="50" r="38" stroke-dasharray="4 7"/>
  </g>
  <circle cx="50" cy="52" r="6" fill="#7dd3fc" stroke="none"/>
</svg>
"""

# The one template that is a *specific* face rather than an archetype, and the
# reason it is checked in: `avatars/` is gitignored, so without this a reclone
# would leave the "big yahu" wake phrase — which now lives on that avatar and
# nowhere else — with no avatar to belong to. A template carries art, not
# identity, so `jarvis avatar new bibi --template bibi` still needs
# `"wake": ["big yahu", "bibi"]` written into the avatar.json it scaffolds.
_BIBI = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <g fill="none" stroke="#7dd3fc" stroke-width="2.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M31 31 C31 18 39 11 50 11 C61 11 69 18 69 31
             C69 45 64 56 50 59 C36 56 31 45 31 31 Z"/>
    <path d="M32 30 C33 18 41 13 50 15 C60 13 67 19 68 30"/>
    <path d="M35 27 C41 22 50 21 56 23 C61 25 64 27 66 30"/>
    <path d="M31 33 C27 33 27 40 31 40"/>
    <path d="M69 33 C73 33 73 40 69 40"/>
    <path d="M38 34 L46 35"/>
    <path d="M62 34 L54 35"/>
    <path d="M50 41 L50 47 L46 49"/>
    <path d="M43 53 C47 55 53 55 57 53"/>
    <path d="M16 92 C16 75 29 65 42 62"/>
    <path d="M84 92 C84 75 71 65 58 62"/>
    <path d="M42 62 L50 71 L58 62"/>
    <path d="M50 71 L45 77 L50 92 L55 77 Z"/>
  </g>
  <g fill="#7dd3fc" stroke="none">
    <circle cx="42" cy="39" r="2.6"/>
    <circle cx="58" cy="39" r="2.6"/>
  </g>
  <g fill="none" stroke="#38bdf8" stroke-width="1.5" opacity=".55">
    <path d="M26 88 L74 88"/>
  </g>
</svg>
"""

TEMPLATES: dict[str, tuple[str, str, str]] = {
    "fox": ("A fox's head, ears up.", "#f59e0b", _FOX),
    "owl": ("An owl, wide-eyed.", "#a78bfa", _OWL),
    "bust": ("A head and shoulders in silhouette.", "#7dd3fc", _BUST),
    "reactor": ("The arc reactor, as a standalone face.", "#38bdf8", _REACTOR),
    "bibi": ("Benjamin Netanyahu, in line art — suit, tie, combed back.",
             "#3b82f6", _BIBI),
}
