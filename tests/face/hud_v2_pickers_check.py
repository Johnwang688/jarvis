"""B-PICKERS in real Chromium over the isolated mock HUD and a mocked Remote socket.

The mock draws a page with a select, a date and a colour control into its
frames and answers the owner's own left click on one of them with the
daemon's `{t:"picker"}` (as `jarvis/v2/browser/pickers.py` does after the
native popup opened). It checks the overlay is shown only for the owner's
press, never takes the keyboard, routes keys to the picker and not the page,
consumes a dismissing press, closes under an approval card, stays inside its
pane at 375 px, and that nothing reaches a control listener. Screenshots go
to `JARVIS_PICKERS_SHOTS` when it is set.
"""
from pathlib import Path
import io, json, os, struct, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_remote_check import Remote
from tests.face.hud_v2_layout_check import _boot, _pane, _set_ws, _approval

CANARY = "PICKER_LABEL_PRIVATE"
# Page CSS px (the frame's viewport), the controls the mock page draws.
ZONES = {"select": (40, 40, 220, 28), "date": (40, 110, 220, 28), "color": (40, 180, 220, 28)}
LABELS = {"select": "Country", "date": "Departure", "color": "Accent"}


def picker_for(kind, n):
    x, y, w, h = ZONES[kind]
    rect = {"x": x, "y": y, "width": w, "height": h}
    pid = f"{n:08x}"
    if kind == "select":
        names = ["Australia", "Canada", "Denmark", "Estonia", "France", "Germany", "Iceland", "Japan", "Norway"]
        items = [{"i": 0, "label": "Choose a country…", "disabled": True, "g": -1}]
        items += [{"i": i + 1, "label": name, "disabled": name == "Estonia", "g": 0 if i < 5 else 1}
                  for i, name in enumerate(names)]
        items.append({"i": len(names) + 1, "label": f"{CANARY} ‮evil <b>bold</b>", "disabled": False, "g": -1})
        return {"t": "picker", "id": pid, "kind": "select", "rect": rect, "items": items,
                "groups": ["Popular", "Nordic and more"], "selected": 2, "total": len(items), "shown": len(items)}
    if kind == "date":
        return {"t": "picker", "id": pid, "kind": "date", "rect": rect, "value": "2026-10-16",
                "min": "2026-01-01", "max": "2027-12-31", "step": "", "required": False}
    return {"t": "picker", "id": pid, "kind": "color", "rect": rect, "value": "#6fc3df"}


class PickerRemote(Remote):
    def __init__(self, ctx, mock):
        super().__init__(ctx, mock)
        self.next = 0x1000
        self.offered = []
        self.pressed = None

    def frame(self, sock, width, height, pixels=None):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", pixels or (width, height), (246, 246, 243))
        draw = ImageDraw.Draw(image)
        draw.text((40, 12), "Booking form (mock page)", fill=(30, 30, 30))
        for kind, (x, y, w, h) in ZONES.items():
            draw.text((x, y - 14), LABELS[kind], fill=(60, 60, 60))
            draw.rectangle((x, y, x + w, y + h), outline=(120, 120, 120), fill=(255, 255, 255))
            text = {"select": "Canada", "date": "10/16/2026", "color": ""}[kind]
            if kind == "color":
                draw.rectangle((x + 4, y + 4, x + 40, y + h - 4), fill=(111, 195, 223))
            draw.text((x + 8, y + 8), text, fill=(20, 20, 20))
            if kind != "color":
                draw.text((x + w - 16, y + 8), "v" if kind == "select" else "#", fill=(80, 80, 80))
        out = io.BytesIO()
        image.save(out, "JPEG")
        sock["frames"] += 1
        sock["ws"].send(struct.pack("!IHH", sock["frames"], width, height) + out.getvalue())

    def receive(self, sock, text):
        super().receive(sock, text)
        message = json.loads(text)
        if message.get("t") == "mouse" and message["e"].get("button") == "left":
            e = message["e"]
            if e["type"] == "mousePressed":
                self.pressed = (e["x"], e["y"])
            elif e["type"] == "mouseReleased" and self.pressed:
                px, py = self.pressed
                self.pressed = None
                for kind, (x, y, w, h) in ZONES.items():
                    if x <= px <= x + w and y <= py <= y + h:
                        self.next += 1
                        offer = picker_for(kind, self.next)
                        self.offered.append(offer)
                        self.send(sock, offer)
        elif message.get("t") == "pick":
            self.send(sock, {"t": "picker_closed", "id": message["id"], "reason": "applied"})


def pickers_checks(browser, mock, base, check, until, guard, init_script):
    shots = os.environ.get("JARVIS_PICKERS_SHOTS")

    def shot(page, name):
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"{name}.png"))

    def required(predicate, timeout=5):
        def poll():
            page.evaluate("0")
            return predicate()
        assert until(poll, timeout=timeout), "required picker prerequisite did not settle"
        return True

    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, permissions=["microphone"])
    guard(ctx); ctx.add_init_script(init_script)
    remote = PickerRemote(ctx, mock)
    page = ctx.new_page(); page.set_default_timeout(5000); errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _boot(page, mock, base, required)
        raw = {"preset": "cols2", "focused": 1, "panes": [{"view": "chat"}, {"view": "browser", "browserProfile": "p-11111111",
               "browser": {"active": "pageaa", "tabs": [{"id": "pageaa", "mode": "remote", "url": "https://example.test/booking"}]}}]}
        _set_ws(page, json.dumps(raw)); _boot(page, mock, None, required, reload=True)
        active = _pane(2, '[data-browser-page][aria-hidden="false"]')
        canvas = page.locator(active + " canvas"); proxy = page.locator(active + ' [data-testid="remote-input"]')
        overlay = page.locator(active + ' [data-testid="remote-picker"]')
        required(lambda: canvas.get_attribute("data-frame") == "ready"); sock = remote.sockets[-1]

        def sent(kind):
            return [m for m in sock["messages"] if m.get("t") == kind]

        def click_zone(kind, dx=20):
            x, y, w, h = ZONES[kind]
            target = (x + (w - 12 if kind != "select" and dx is None else dx), y + h / 2)
            page.mouse.move(*_abs(canvas, target)); page.mouse.click(*_abs(canvas, target))

        chat = page.locator(_pane(1, '[data-testid="input"]')); chat.click(); chat.fill("retained draft")
        remote.send(sock, picker_for("select", 0xdead))
        page.wait_for_timeout(300)
        check("an unsolicited picker is never shown", overlay.count() == 0)
        check("an unsolicited picker never moves the keyboard", chat.evaluate("e=>e===document.activeElement"))
        check("an unsolicited picker is answered with its cancel", any(m["id"] == f"{0xdead:08x}" for m in sent("pick_cancel")))

        click_zone("select")
        required(lambda: overlay.count() == 1)
        check("the owner's click on a select shows its picker", overlay.get_attribute("data-kind") == "select")
        check("the picker never takes the keyboard: it stays in the Remote proxy",
              proxy.evaluate("e=>e===document.activeElement"))
        box = overlay.bounding_box(); cbox = canvas.bounding_box()
        x, y, w, h = ZONES["select"]
        check("the picker opens just under its control",
              abs(box["x"] - (cbox["x"] + x)) <= 2 and abs(box["y"] - (cbox["y"] + y + h)) <= 2, json.dumps(box))
        check("the picker stays inside its Remote surface",
              box["x"] >= cbox["x"] - 1 and box["y"] + box["height"] <= cbox["y"] + cbox["height"] + 1)
        texts = overlay.locator('[role="option"]').all_text_contents()
        check("page labels are text, cleaned, with groups shown",
              any(t.endswith("evil <b>bold</b>") and "‮" not in t for t in texts)
              and overlay.locator("b").count() == 0 and "Nordic and more" in overlay.inner_text())
        check("the current option is marked and highlighted",
              overlay.locator('[aria-selected="true"]').inner_text().strip().endswith("Canada"))
        shot(page, "select-overlay")
        before_keys = len(sent("key"))
        page.keyboard.press("ArrowDown"); page.keyboard.press("ArrowDown")
        check("arrow keys skip a disabled option",
              overlay.locator('[aria-selected="true"]').inner_text().strip().endswith("France"))
        page.keyboard.press("n")
        check("type-ahead moves to a matching option", overlay.locator('[aria-selected="true"]').inner_text().strip().endswith("Norway"))
        shot(page, "select-overlay-keyboard")
        page.keyboard.press("Enter")
        required(lambda: overlay.count() == 0)
        check("Enter applies the highlighted option by its index", sent("pick")[-1:] == [{"t": "pick", "id": remote.offered[-1]["id"], "index": 9}])
        check("keys typed while the picker was open never reached the page", len(sent("key")) == before_keys)
        check("the keyboard is back in the Remote proxy", proxy.evaluate("e=>e===document.activeElement"))

        click_zone("select"); required(lambda: overlay.count() == 1)
        mouse_before = len(sent("mouse"))
        cbox = canvas.bounding_box(); away = (cbox["width"] - 40, cbox["height"] - 40)
        assert page.evaluate("""([x,y])=>!!document.elementFromPoint(x,y)?.closest('[data-testid="remote-surface"]')
          &&!document.elementFromPoint(x,y)?.closest('[data-testid="remote-picker"]')""", list(_abs(canvas, away))), "dismissal must hit the page outside the picker"
        page.mouse.click(*_abs(canvas, away))
        required(lambda: overlay.count() == 0)
        check("a press on the page closes the picker and is consumed whole",
              not [m for m in sent("mouse")[mouse_before:] if m["e"]["type"] != "mouseMoved"] and sent("pick_cancel")[-1]["id"] == remote.offered[-1]["id"])
        page.mouse.click(*_abs(canvas, away))
        required(lambda: len(sent("mouse")) > mouse_before)
        check("the next deliberate click reaches the page", any(m["e"]["type"] == "mouseReleased" for m in sent("mouse")[mouse_before:]))

        click_zone("date", None); required(lambda: overlay.count() == 1)
        field = overlay.locator('[data-testid="remote-picker-input"]')
        check("a date picker offers the HUD's own date input, unfocused",
              field.get_attribute("type") == "date" and field.input_value() == "2026-10-16"
              and proxy.evaluate("e=>e===document.activeElement"))
        shot(page, "date-overlay")
        field.click()
        check("the owner's click puts the keyboard in the date input", field.evaluate("e=>e===document.activeElement"))
        field.fill("2026-12-24")
        overlay.locator('[data-testid="remote-picker-set"]').click()
        required(lambda: overlay.count() == 0)
        check("Set applies the chosen date", sent("pick")[-1] == {"t": "pick", "id": remote.offered[-1]["id"], "value": "2026-12-24"})
        check("after Set the keyboard returns to the Remote proxy", proxy.evaluate("e=>e===document.activeElement"))

        click_zone("color"); required(lambda: overlay.count() == 1)
        check("a colour picker offers the HUD's own colour input",
              overlay.locator('[data-testid="remote-picker-input"]').get_attribute("type") == "color")
        shot(page, "color-overlay")
        overlay.locator('[data-testid="remote-picker-cancel"]').click()
        required(lambda: overlay.count() == 0)
        check("Cancel tells the daemon", sent("pick_cancel")[-1]["id"] == remote.offered[-1]["id"])

        click_zone("select"); required(lambda: overlay.count() == 1)
        _approval(mock, "picker-card"); required(lambda: page.locator('[data-testid="approval-card"]').count() == 1)
        until(lambda: page.evaluate("0") is None and overlay.count() == 0, timeout=3)
        check("an approval card closes the picker: never drawn under a card", overlay.count() == 0)
        shot(page, "card-closes-picker")
        remote.send(sock, picker_for("select", 0xbeef))
        page.wait_for_timeout(200)
        check("a picker arriving under a card is not shown", overlay.count() == 0)
        page.keyboard.press("Escape")
        required(lambda: page.locator('[data-testid="approval-card"]').count() == 0)
        check("the picker does not come back after the card", overlay.count() == 0)

        canvas.click(position={"x": 300, "y": 300})
        page.set_viewport_size({"width": 375, "height": 812})
        required(lambda: canvas.get_attribute("data-frame") == "ready")
        required(lambda: page.locator(active).count() == 1 and canvas.bounding_box() is not None)
        click_zone("select"); required(lambda: overlay.count() == 1)
        box = overlay.bounding_box(); cbox = canvas.bounding_box()
        check("at 375 px the picker stays inside its visible pane",
              box["x"] >= cbox["x"] - 1 and box["x"] + box["width"] <= min(375, cbox["x"] + cbox["width"]) + 1
              and box["y"] + box["height"] <= cbox["y"] + cbox["height"] + 1, json.dumps([box, cbox]))
        shot(page, "select-overlay-375")
        page.keyboard.press("Escape"); required(lambda: overlay.count() == 0)
        check("pickers use no control listener", not mock.api_hits)
        check("picker labels never reach browser storage", CANARY not in page.evaluate("JSON.stringify(localStorage)+JSON.stringify(sessionStorage)"))
        check("pickers produce no page errors", not errors, str(errors[:3]))
    finally:
        ctx.close()


def _abs(canvas, point):
    box = canvas.bounding_box()
    return box["x"] + point[0], box["y"] + point[1]


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            try:
                pickers_checks(browser, mock, f"http://127.0.0.1:{mock.port}", v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
            except Exception as error:
                import traceback
                traceback.print_exc()
                v2.check("pickers scenario completes", False, str(error))
            finally:
                browser.close()
    finally:
        mock.stop()
    print(f"{v2.CHECKS} picker checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)


if __name__ == "__main__":
    sys.exit(main())
