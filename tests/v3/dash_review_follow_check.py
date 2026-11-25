"""A real wheel scroll can coincide with a transcript resize in the same frame."""

import unittest

import e2e_window_check as window


AFTER_LAYOUT = "() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)))"


class Follow(window.Window):
    def test_wheel_scroll_during_resize_takes_the_reader_off_the_end(self):
        self.start_daemon(self.reply("Fixture transcript line.\n" * 160))
        self.open_window()
        self.send("Fill the transcript.")
        self.expect_idle()
        self.page.evaluate(AFTER_LAYOUT)
        distance = "e => e.scrollHeight - e.clientHeight - e.scrollTop"
        self.assertLessEqual(self.transcript.evaluate(distance), 1)
        box = self.transcript.bounding_box()
        self.page.mouse.move(box["x"] + 30, box["y"] + 30)
        self.transcript.evaluate("""log => {
            log.addEventListener("scroll", () => {
                const height = log.clientHeight;
                const distance = log.scrollHeight - height - log.scrollTop;
                // Place the resize before React receives the wheel-generated scroll.
                document.querySelector(".chat-composer textarea").style.height = "110px";
                log.dataset.wheelSample = JSON.stringify([distance, height, log.clientHeight]);
            }, {capture: true, once: true});
        }""")
        self.page.mouse.wheel(0, -230)
        self.page.wait_for_function("() => document.querySelector('.chat-log').dataset.wheelSample")
        delta, before, after = self.transcript.evaluate("e => JSON.parse(e.dataset.wheelSample)")
        self.assertGreater(delta, 100, "The wheel must have moved the transcript")
        self.assertLess(after, before, "The resize must have changed the box")
        self.page.evaluate(AFTER_LAYOUT)
        self.assertGreater(self.transcript.evaluate(distance), 100,
                           "The resize sent the reader back to the end after a real wheel scroll")


for name in dir(window.Window):
    if name.startswith("test_"):
        setattr(Follow, name, None)


if __name__ == "__main__":
    unittest.main()
