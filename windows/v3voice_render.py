"""Draw the Windows voice pop-up from display state alone."""

from dataclasses import dataclass, replace
from functools import lru_cache
from math import cos, degrees, isfinite, tau
import os
from pathlib import Path
from unicodedata import category

from PIL import Image, ImageDraw, ImageFilter, ImageFont


CYAN = (111, 195, 223)
CYAN_HI = (154, 214, 234)
AMBER = (251, 191, 36)
RED = (248, 113, 113)
TEXT = (234, 242, 246)
TEXT_SECONDARY = (196, 208, 216)
MUTED = (159, 176, 186)
APPROVALS = ("asking", "needs_click")

RESTING_TEXT = 'Say “Jarvis” · or hold Right Ctrl'
SPEAKING_HINT = 'Say “stop” or press the key to interrupt'
ASKING_HINT = 'Say “yes” or “no”, or click a button.'
WAITING_HINT = 'Click a button, or wait for the prompt.'
CLICK_HINT = 'Voice can’t approve this one. Click here or use your phone.'
FINISHED_HINT = 'Ask “what happened?” for more'
PROBLEM_TEXT = 'The microphone isn’t available.'
PROBLEM_HINT = 'Another app may be holding it'
LABELS = {
    "listening": "Listening", "working": "Working", "speaking": "Jarvis",
    "asking": "Approve?", "needs_click": "Needs a click", "finished": "Finished",
    "problem": "Can’t hear you",
}
HINTS = {
    "speaking": SPEAKING_HINT, "asking": ASKING_HINT, "needs_click": CLICK_HINT,
    "finished": FINISHED_HINT, "problem": PROBLEM_HINT,
}


@dataclass(frozen=True)
class PopupState:
    kind: str = "hidden"
    label: str = ""
    line: str = ""
    sub: str = ""
    level: float = 0.0
    expires: float = 0.0
    buttons: bool = False
    t: float = 0.0
    id: str = ""
    digest: str = ""
    click_live: bool | None = None


def _clean(value):
    if type(value) is not str:
        return ""
    # Bidi overrides and other format controls must never reach text shaping.
    # Bound the amount of text that a malformed link event can give the renderer.
    value, cut = value[:600], len(value) > 600
    text = "".join(" " if char.isspace() else char for char in value
                   if category(char) not in ("Cc", "Cf", "Cs") or char.isspace())
    text = " ".join(text.split())
    return text + "…" if cut else text


def _fraction(value):
    if type(value) in (float, int) and 0 < value < float("inf"):
        return min(1.0, value)
    return 0.0


def valid_duration(seconds):
    return type(seconds) in (int, float) and 0 < seconds <= 600


def buttons_enabled(state):
    live = _fraction(state.expires) > 0 if state.click_live is None else state.click_live
    return state.kind in APPROVALS and state.buttons is True and live


_EVENT_TEXT = {
    "state": ("state", "reason"), "partial": ("text",), "heard": ("text",),
    "say": ("text",), "asking": ("sentence", "tool", "id", "digest"),
    "error": ("reason",), "notice": ("title", "text"),
}


def state_from_event(previous, event):
    """Translate decoded EVENT frames. The link driver owns display lifetimes.

    An approval's seconds start the display at a full bar. The window can take
    the same duration from the driver when the state is published.
    """
    def show(kind, line="", *, label=None, buttons=False, card_id="", digest=""):
        return PopupState(kind=kind, label=LABELS.get(kind, "") if label is None else label,
                          line=line, sub=HINTS.get(kind, ""), level=previous.level,
                          expires=float(buttons), buttons=buttons, t=previous.t,
                          id=card_id, digest=digest)

    if type(event) is not dict:
        return previous
    kind = event.get("kind")
    if type(kind) is not str:
        return previous
    fields = _EVENT_TEXT.get(kind)
    if fields is None or any(key in event and type(event[key]) is not str for key in fields):
        return previous
    if kind == "state":
        kind = event.get("state")
        if kind == previous.kind or (kind == "asking" and previous.kind == "needs_click"):
            return previous
        if kind == "unavailable":
            return show("problem", _clean(event.get("reason")) or PROBLEM_TEXT)
        if kind == "resting":
            return show("resting", RESTING_TEXT)
        if kind in ("hidden", "listening", "working", "speaking", *APPROVALS):
            return show(kind)
    elif kind in ("partial", "heard", "say"):
        if "text" not in event or (previous.kind in APPROVALS and kind in ("partial", "say")):
            return previous  # The approval remains visible while its sentence plays.
        message = _clean(event.get("text"))
        if kind == "partial":
            return show("listening", message)
        if kind == "heard":
            return show("working", message)
        return show("speaking", message)
    elif kind == "asking":
        remaining = event.get("expires_in", 0)
        click_remaining = event.get("click_expires_in", remaining)
        voice_allowed = event.get("voice_allowed")
        waiting = event.get("voice_waiting", False)
        if (type(remaining) not in (int, float)
                or type(click_remaining) not in (int, float) or type(waiting) is not bool
                or ("voice_allowed" in event and type(voice_allowed) is not bool)):
            return previous
        click_only = voice_allowed is False or (voice_allowed is not True and previous.kind == "needs_click")
        kind = "needs_click" if click_only else "asking"
        sentence = _clean(event.get("sentence")) or _clean(event.get("tool"))
        state = show(kind, sentence, buttons=valid_duration(click_remaining),
                     card_id=event.get("id", ""), digest=event.get("digest", ""))
        if "click_expires_in" in event:
            state = replace(state, click_live=state.buttons, expires=float(valid_duration(remaining)))
        if waiting and not click_only:
            state = replace(state, sub=WAITING_HINT)
        return state
    elif kind == "error":
        return show("problem", _clean(event.get("reason")) or PROBLEM_TEXT)
    elif kind == "notice" and "text" in event:
        title = _clean(event.get("title"))
        label = f"Finished · {title}" if title else LABELS["finished"]
        return show("finished", _clean(event.get("text")), label=label)
    return previous


@lru_cache(maxsize=64)
def _font(paths, pixels, bold):
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = ("seguisb.ttf", "SegoeUIVariable.ttf", "segoeui.ttf") if bold else (
        "SegoeUIVariable.ttf", "segoeui.ttf")
    fallback = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in (*paths, *(fonts / name for name in names), fallback):
        try:
            return ImageFont.truetype(path, pixels)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=pixels)
    except TypeError:  # Older Pillow provides a fixed-size bitmap font.
        return ImageFont.load_default()


def _glyph(font, char):
    mask = font.getmask(char, mode="L")
    return mask.size, bytes(mask)


@lru_cache(maxsize=64)
def _missing_glyph(font):
    # A Unicode noncharacter exposes the font's missing-glyph bitmap.
    try:
        return _glyph(font, "\U0010ffff")
    except UnicodeEncodeError:
        return None


@lru_cache(maxsize=4096)
def _has_glyph(font, char):
    try:
        return _glyph(font, char) != _missing_glyph(font)
    except UnicodeEncodeError:
        return False


def _display_text(text, font):
    chars = []
    marks = 0
    for char in _clean(text):
        if category(char).startswith("M"):
            marks += 1
            if marks > 2:
                continue
        else:
            marks = 0
        chars.append(char if char == " " or _has_glyph(font, char) else " ")
    return "".join(chars)


def _wrap(text, font, width, limit):
    """Fit measured glyphs, splitting an oversized word when needed."""
    lines = []
    text = _display_text(text, font)
    measure = font.getlength
    while text and len(lines) < limit:
        if measure(text) <= width:
            lines.append(text)
            break
        last = len(lines) == limit - 1
        suffix = ("…" if _has_glyph(font, "…") else "...") if last else ""
        if measure(suffix) > width:
            break
        left, right = 0, len(text)
        while left < right:
            mid = (left + right + 1) // 2
            if measure(text[:mid].rstrip() + suffix) <= width:
                left = mid
            else:
                right = mid - 1
        split = text.rfind(" ", 0, left + 1) if left < len(text) else left
        # On the final line use all available space before the ellipsis.
        end = left if last or split <= 0 else split
        lines.append(text[:end].rstrip() + suffix)
        text = text[max(1, end):].lstrip()
    return tuple(lines)


@dataclass(frozen=True)
class _Layout:
    pixels: tuple[int, int]
    panel: tuple[float, float]
    orb: tuple[float, float, float] = (0, 0, 0)
    body: tuple[float, float] = (0, 0)
    label: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    sub: tuple[str, ...] = ()
    hint: tuple[str, ...] = ()
    hint_top: float = 0
    # The two rectangles are snapped in output pixels for drawing and hit testing.
    buttons: tuple[tuple[str, tuple[int, int, int, int]], ...] = ()
    body_width: float = 0
    hint_width: float = 0


def _layout(state, scale, width, paths):
    if scale <= 0 or width <= 0 or not isfinite(scale) or not isfinite(width):
        raise ValueError("scale and width must be positive and finite")
    if state.kind == "hidden":
        return _Layout((1, 1), (0, 0))

    def measured(text, points, available, limit=1, *, bold=False):
        font = _font(paths, max(1, round(points * scale * 2)), bold)
        return _wrap(text, font, max(0, available - 2) * scale * 2, limit)

    def output(w, h):
        return max(1, round(w * scale)), max(1, round(h * scale))

    if state.kind == "resting":
        w, h = min(width, 250), 38
        lines = measured(_clean(state.line) or RESTING_TEXT, 12.5, w - 54)
        return _Layout(output(w, h), (w, h), (10, 8, 22), (41, 11), lines=lines,
                       body_width=w - 54)

    w = min(width, 470)
    trailing = {"listening": 51, "speaking": 39, "working": 28, "finished": 22}.get(state.kind, 0)
    available = w - 71 - 15 - (trailing + 12 if trailing else 0)
    label = measured((_clean(state.label) or LABELS.get(state.kind, "")).upper(), 11, available, bold=True)
    lines = measured(state.line, 14, available, 2)
    approval = state.kind in APPROVALS
    sub = () if approval else measured(state.sub, 12, available)
    body_h = 13 + (2 + 19 * len(lines) if lines else 0) + (3 + 15 if sub else 0)
    row_h = max(46, body_h)
    center = 12 + row_h / 2
    orb = (13, center - 23, 46)
    body = (71, center - body_h / 2)
    if not approval:
        h = 24 + row_h
        return _Layout(output(w, h), (w, h), orb, body, label, lines, sub, body_width=available)

    row_top = 12 + row_h + 10
    buttons = ()
    hint_width = w - 28
    if state.buttons is True and w >= 184:
        rectangles = (("deny", (w - 169, row_top, w - 107, row_top + 33)),
                      ("approve", (w - 99, row_top, w - 15, row_top + 33)))
        buttons = tuple((name, tuple(round(coord * scale) for coord in box))
                        for name, box in rectangles)
        hint_width -= 164
    hint = measured(_clean(state.sub) or HINTS[state.kind], 12, hint_width, 2)
    second_h = max(33 if buttons else 15, 15 * len(hint))
    h = row_top + second_h + 10 + 3 + 1
    return _Layout(output(w, h), (w, h), orb, body, label, lines, hint=hint,
                   hint_top=row_top + (second_h - 15 * len(hint)) / 2, buttons=buttons,
                   body_width=available, hint_width=hint_width)


def size(state, *, scale=1.0, width=470, font_paths=()):
    return _layout(state, scale, width, tuple(font_paths)).pixels


def hit_test(state, x, y, *, scale=1.0, width=470, font_paths=()):
    if not buttons_enabled(state):
        return None
    for action, (left, top, right, bottom) in _layout(state, scale, width, tuple(font_paths)).buttons:
        if left <= x < right and top <= y < bottom:
            return action
    return None


# The dashboard's DEFAULT_RINGS with the 120-tick ring omitted. Angles are radians.
_RINGS = (
    (.99, .75, 0, .35, ((0, tau),)),
    (.845, 5, -.08, .32, ((0, .5), (.6, .75), (.85, 1.9), (2.2, 2.35),
                         (2.45, 3.6), (3.9, 4.6), (4.7, 4.8), (4.95, 5.9))),
    (.77, 1, .2, .6, ((.3, 2.6), (3.4, 5.5))),
    (.7, 2.5, -.14, .42, tuple((i * tau / 60, (i + .5) * tau / 60) for i in range(60))),
    (.6, 1, .3, .45, ((0, 2.4), (2.9, 5.6))),
)
_PALETTES = {
    "listening": (CYAN_HI, 1.35, 1.6), "working": (CYAN, 1.15, 3.4),
    "speaking": (CYAN_HI, 1.5, 1.2), "asking": (AMBER, 1.5, .35),
    "needs_click": (AMBER, 1.5, .35), "problem": (RED, 1.3, .6),
}


def _orb(image, orb, kind, t, factor):
    x, y, diameter = orb
    side = max(1, round(diameter * factor))
    center, radius = side / 2, side * .46
    k = max(.3, diameter / 130)
    hue, boost, spin = _PALETTES.get(kind, (CYAN, .85, .8))
    sprite = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(sprite)

    def arc(draw, r, start, end, weight, ink):
        stroke = max(1, round(weight * factor))
        outer = radius * r + (stroke - 1) / 2
        box = (center - outer, center - outer, center + outer, center + outer)
        if end - start >= tau:
            draw.ellipse(box, outline=ink, width=stroke)
        else:
            angle = degrees(start % tau)
            draw.arc(box, angle, angle + degrees(end - start), fill=ink, width=stroke)

    for r, w, speed, alpha, arcs in _RINGS:
        rot = t * speed * spin
        ink = (*hue, round(255 * min(1, alpha * boost * 1.25)))
        for start, end in arcs:
            arc(draw, r, start + rot, end + rot, max(.6, w * k), ink)

    core = Image.new("L", sprite.size)
    draw = ImageDraw.Draw(core)
    arc(draw, .17, 0, tau, max(.9, 2 * k), 230)
    for i in range(3):
        start = t * .24 * spin + i * tau / 3
        arc(draw, .25, start, start + 1.2, max(.7, 1.5 * k), 230)
    glow = Image.new("RGBA", sprite.size, hue)
    glow.putalpha(core.filter(ImageFilter.GaussianBlur(max(3, 15 * k) * factor / 2)))
    emblem = Image.new("RGBA", sprite.size, (232, 238, 240))
    emblem.putalpha(core)
    sprite.alpha_composite(glow)
    sprite.alpha_composite(emblem)
    image.alpha_composite(sprite, (round(x * factor), round(y * factor)))


@lru_cache(maxsize=24)
def _glass(dimensions, radius, rim, factor):
    w, h = dimensions
    outline = (0, 0, w - 1, h - 1)
    radius = round(radius * factor)
    mask = Image.new("L", dimensions)
    ImageDraw.Draw(mask).rounded_rectangle(outline, radius, fill=255)
    image = Image.new("RGBA", dimensions, (22, 33, 42, 219))
    sheen = Image.new("RGBA", dimensions)
    draw = ImageDraw.Draw(sheen)
    for y in range(h):
        alpha = round(23 * max(0, 1 - y / max(1, .7 * h)) ** 2)
        draw.line((0, y, w, y), fill=(255, 255, 255, alpha))
    draw.rounded_rectangle(outline, radius, outline=rim, width=max(1, round(factor)))
    image.alpha_composite(sheen)
    image.putalpha(Image.composite(image.getchannel("A"), Image.new("L", dimensions), mask))
    return image, mask


def render(state, *, scale=1.0, width=470, font_paths=()):
    """Return a straight-alpha RGBA frame, supersampled at twice the output DPI."""
    paths = tuple(font_paths)
    layout = _layout(state, scale, width, paths)
    if state.kind == "hidden":
        return Image.new("RGBA", layout.pixels)
    factor = scale * 2
    w, h = layout.panel
    pixels = tuple(n * 2 for n in layout.pixels)
    kind = state.kind
    t = state.t if isfinite(state.t) else 0
    approval = kind in APPROVALS
    accent = AMBER if approval else RED if kind == "problem" else CYAN_HI
    rim = (*accent, 140 if approval or kind == "problem" else 48)
    glass, mask = _glass(pixels, h / 2 if kind == "resting" else 18, rim, factor)
    image = glass.copy()
    _orb(image, layout.orb, kind, t, factor)
    overlay = Image.new("RGBA", pixels)
    draw = ImageDraw.Draw(overlay)
    text_layers = []

    def box(*coordinates):
        return tuple(round(coord * factor) for coord in coordinates)

    def text(lines, bounds, points, color, step, *, bold=False, anchor="lt"):
        if not lines:
            return
        left, top, right, bottom = (round(coord * scale) for coord in bounds)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return
        # Supersample each block separately so the final resize cannot spread
        # ink outside its rectangle, even with tall marks or glyph overhangs.
        layer = Image.new("RGBA", (width * 2, height * 2))
        ink = ImageDraw.Draw(layer)
        font = _font(paths, max(1, round(points * factor)), bold)
        x = width if anchor == "mm" else 0
        y = height if anchor in ("lm", "mm") else 0
        for index, line in enumerate(lines):
            ink.text((x, y + round(step * index * factor)), line,
                     font=font, fill=color, anchor=anchor)
        text_layers.append((layer.resize((width, height), Image.Resampling.LANCZOS), (left, top)))

    x, y = layout.body
    right = x + layout.body_width
    if kind == "resting":
        text(layout.lines, (x, y, right, y + 16), 12.5, TEXT_SECONDARY, 15, anchor="lm")
    else:
        line_bottom = y + 15 + 19 * len(layout.lines)
        text(layout.label, (x, y, right, y + 13), 11, accent, 13, bold=True)
        text(layout.lines, (x, y + 15, right, line_bottom), 14, TEXT, 19)
        text(layout.sub, (x, line_bottom + 3, right, line_bottom + 18), 12, MUTED, 15)

    center = layout.orb[1] + layout.orb[2] / 2
    if kind in ("listening", "speaking"):
        phases = (0, .9, .3, .7, .1, .5, .8, .2, .6)
        phases = phases if kind == "listening" else phases[:7]
        level = _fraction(state.level)
        for i, phase in enumerate(phases):
            x = w - 15 - (len(phases) - i) * 6 + 3
            pulse = .5 + .5 * cos(tau * (t + phase) / 1.1)
            half = 2 + level * (2.5 + 10.5 * pulse)
            color = CYAN if kind == "listening" else (*CYAN_HI, 217)
            draw.rounded_rectangle(box(x, center - half, x + 3, center + half),
                                   round(1.5 * factor), fill=color)
    elif kind == "working":
        for i in range(3):
            x = w - 43 + i * 11
            alpha = round(140 + 115 * cos(tau * (t - i * .15) / 1.2))
            draw.ellipse(box(x, center - 3, x + 6, center + 3), fill=(*CYAN, alpha))
    elif kind == "finished":
        x = w - 37
        draw.ellipse(box(x, center - 11, x + 22, center + 11), fill=(*CYAN, 41),
                     outline=(*CYAN, 128), width=max(1, round(factor)))
        draw.line([box(x + 6, center), box(x + 10, center + 4), box(x + 16, center - 5)],
                  fill=CYAN_HI, width=max(1, round(2 * factor)), joint="curve")
    elif approval:
        hint_bottom = layout.hint_top + 15 * len(layout.hint)
        text(layout.hint, (13, layout.hint_top, 13 + layout.hint_width, hint_bottom), 12, MUTED, 15)
        enabled = buttons_enabled(state)
        button_font = _font(paths, max(1, round(13 * factor)), True)
        for action, (left, top, right, bottom) in layout.buttons:
            approve = action == "approve" and enabled
            draw.rounded_rectangle((left * 2, top * 2, right * 2 - 1, bottom * 2 - 1),
                                   round(9 * factor), fill=AMBER if approve else (255, 255, 255, 15),
                                   outline=AMBER if approve else (255, 255, 255, 41),
                                   width=max(1, round(factor)))
            color = (27, 20, 3) if approve else TEXT if enabled else MUTED
            bounds = tuple(coord / scale for coord in (left, top, right, bottom))
            text((_display_text(action.title(), button_font),), bounds, 13, color, 13,
                 bold=True, anchor="mm")
        filled = round(pixels[0] * _fraction(state.expires))
        if layout.buttons or filled:
            bar_top = pixels[1] - round(4 * factor)
            bar_bottom = pixels[1] - round(factor) - 1
            draw.rectangle((0, bar_top, pixels[0] - 1, bar_bottom), fill=(*AMBER, 41))
            if filled:
                draw.rectangle((0, bar_top, filled - 1, bar_bottom), fill=AMBER)

    image.alpha_composite(overlay)
    # Clip all decoration to the same rounded silhouette, including the countdown.
    image.putalpha(Image.composite(image.getchannel("A"), Image.new("L", pixels), mask))
    image = image.resize(layout.pixels, Image.Resampling.LANCZOS)
    for layer, position in text_layers:
        image.alpha_composite(layer, position)
    return image
