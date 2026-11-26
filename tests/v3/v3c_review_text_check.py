from dataclasses import replace
import unittest
from unittest.mock import patch

from PIL import ImageChops

from voice_popup_check import SAMPLES, drawing


class Text(unittest.TestCase):
    def test_transcript_ink_cannot_overprint_the_approval_hint(self):
        state = replace(SAMPLES["asking"], line="A" + "\u0301" * 25)
        scale = 2
        layout = drawing._layout(state, scale, 470, ())
        frame = drawing.render(state, scale=scale).convert("RGB")

        def render_layout(variant):
            with patch.object(drawing, "_layout", return_value=variant):
                return drawing.render(state, scale=scale).convert("RGB")

        def visible_change(before, after):
            return ImageChops.difference(before, after).convert("L").point(
                lambda delta: 255 if delta > 24 else 0
            )

        hint_only = render_layout(replace(layout, lines=()))
        blank = render_layout(replace(layout, lines=(), hint=()))
        hint_ink = visible_change(blank, hint_only)
        transcript_change = visible_change(hint_only, frame)
        self.assertIsNotNone(hint_ink.getbbox())
        self.assertIsNone(ImageChops.multiply(hint_ink, transcript_change).getbbox())


if __name__ == "__main__":
    unittest.main()
