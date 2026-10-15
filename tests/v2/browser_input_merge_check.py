"""A page that falls behind gets the newest pointer, not a backlog (stream.py,
2026-10-14), and Chrome draws at RASTER_SCALE. Pure: no Chrome, no listener."""
from pathlib import Path
import json
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from jarvis.v2.browser import host as H
from jarvis.v2.browser.stream import WHEEL_MAX, TabStream, parse


def mouse(kind, x=10, y=20, **e):
    e = {"type": kind, "x": x, "y": y, "button": "none", "buttons": 0, **e}
    return parse(json.dumps({"t": "mouse", "e": e}).encode())


def wheel(dy, x=10, y=20, **e):
    return mouse("mouseWheel", x, y, deltaX=0, deltaY=dy, **e)


def key(kind="rawKeyDown"):
    return parse(json.dumps({"t": "key", "e": {"type": kind, "key": "a", "code": "KeyA",
                                              "windowsVirtualKeyCode": 65}}).encode())


class Merge(unittest.TestCase):
    def setUp(self):
        self.sock = NS(send_text=lambda raw: None)
        self.stream = TabStream("1234abcd", held=lambda: False, wake=lambda: None)
        self.stream._sock = self.sock

    def send(self, *msgs):
        for m in msgs:
            self.stream.enqueue(self.sock, m)

    def drain(self):
        out = []
        while (item := self.stream.take()) is not None:
            out.append(item)
        return out

    def test_queued_moves_become_the_newest(self):
        self.send(mouse("mouseMoved", 1, 1), mouse("mouseMoved", 2, 2), mouse("mouseMoved", 3, 4))
        items = self.drain()
        self.assertEqual([(m.params["x"], m.params["y"]) for _, _, m in items], [(3, 4)])
        self.assertEqual(self.stream.merged, 2)
        self.assertEqual(items[0][0], 3, "the merged event carries the newest sequence number")

    def test_nothing_crosses_a_press_a_release_or_a_key(self):
        self.send(mouse("mouseMoved", 1, 1), mouse("mousePressed", 1, 1, button="left", buttons=1, clickCount=1),
                  mouse("mouseMoved", 2, 2, button="left", buttons=1),
                  mouse("mouseReleased", 2, 2, button="left", clickCount=1),
                  mouse("mouseMoved", 3, 3), key(), mouse("mouseMoved", 4, 4))
        kinds = [m.params.get("type") for _, _, m in self.drain()]
        self.assertEqual(kinds, ["mouseMoved", "mousePressed", "mouseMoved", "mouseReleased",
                                 "mouseMoved", "rawKeyDown", "mouseMoved"])
        self.assertEqual(self.stream.merged, 0)

    def test_different_buttons_or_modifiers_are_kept(self):
        self.send(mouse("mouseMoved", 1, 1), mouse("mouseMoved", 2, 2, button="left", buttons=1),
                  mouse("mouseMoved", 3, 3, button="left", buttons=1, modifiers=8))
        self.assertEqual(len(self.drain()), 3)

    def test_drags_merge_with_drags(self):
        self.send(mouse("mouseMoved", 1, 1, button="left", buttons=1),
                  mouse("mouseMoved", 5, 6, button="left", buttons=1))
        items = self.drain()
        self.assertEqual([(m.params["x"], m.params["buttons"]) for _, _, m in items], [(5, 1)])

    def test_wheels_at_one_point_sum_within_the_bound(self):
        self.send(wheel(100), wheel(-30), wheel(50))
        items = self.drain()
        self.assertEqual([m.params["deltaY"] for _, _, m in items], [120])
        self.assertTrue(items[0][2].takeover, "a merged wheel is still the owner taking over")

    def test_wheels_elsewhere_or_past_the_bound_are_kept(self):
        self.send(wheel(100), wheel(100, x=11))
        self.assertEqual(len(self.drain()), 2)
        self.send(wheel(WHEEL_MAX), wheel(1))
        self.assertEqual([m.params["deltaY"] for _, _, m in self.drain()], [WHEEL_MAX, 1])

    def test_a_taken_event_is_never_merged(self):
        self.send(mouse("mouseMoved", 1, 1))
        first = self.stream.take()
        self.send(mouse("mouseMoved", 2, 2))
        second = self.stream.take()
        self.assertEqual((first[2].params["x"], second[2].params["x"]), (1, 2))

    def test_a_hold_still_drops_a_merged_event(self):
        self.send(mouse("mouseMoved", 1, 1), mouse("mouseMoved", 2, 2))
        self.stream.hold_through(notify=False)
        self.assertIsNone(self.stream.take())
        self.send(mouse("mouseMoved", 3, 3))
        self.assertEqual([m.params["x"] for _, _, m in self.drain()], [3])

    def test_a_full_queue_still_takes_a_move_that_merges(self):
        from jarvis.v2.browser.stream import INPUT_QUEUE_MAX
        sent = []
        self.sock.send_text = lambda raw: sent.append(json.loads(raw))
        for i in range(INPUT_QUEUE_MAX - 1):
            self.send(key("rawKeyDown" if i % 2 == 0 else "keyUp"))
        self.send(mouse("mouseMoved", 1, 1), mouse("mouseMoved", 2, 2))
        self.assertFalse(sent, "the second move merged; nothing was dropped")
        self.send(key())
        self.assertEqual(self.stream.dropped, 1, "a key past the cap is still refused")
        self.assertEqual(self.drain()[-1][2].params["x"], 2)

    def test_wheels_with_other_modifiers_are_kept(self):
        self.send(wheel(100), wheel(100, modifiers=2))
        self.assertEqual(len(self.drain()), 2)

    def test_an_old_attachment_is_never_merged_into(self):
        self.send(wheel(100))
        self.stream.gen += 1
        self.send(wheel(7))
        self.assertEqual([m.params["deltaY"] for _, _, m in self.drain()], [7])


class Raster(unittest.TestCase):
    def test_chrome_draws_at_raster_scale(self):
        args = H.launch_args(Path("/tmp/fixture"), 1234, private=False)
        self.assertEqual(H.RASTER_SCALE, 1.5)
        self.assertIn("--force-device-scale-factor=1.5", args)


if __name__ == "__main__":
    unittest.main()
