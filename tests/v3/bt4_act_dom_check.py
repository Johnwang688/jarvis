"""Exercise the fixed scripts in an isolated world using routed Chromium fixtures."""

import unittest

import bt2_read_dom_check as fixtures
from bt2_read_dom_check import ORIGIN, OTHER, PAGE
from jarvis.v3.browser import act


TYPED = "the exact entered value"


class ActionDOM(unittest.TestCase):
    setUp = fixtures.ReadDOM.setUp
    route = fixtures.ReadDOM.route
    tree = fixtures.ReadDOM.tree
    location = fixtures.ReadDOM.location
    document_read = fixtures.ReadDOM.document_read

    def load(self, html, url=PAGE):
        self.page.route("http://*.example.invalid/**", self.route)
        fixtures.ReadDOM.load(self, html, url)
        return self.document_read(url.rstrip("/"))

    def run_act(self, script, *args, origin=ORIGIN):
        outcome = self.cdp.send("Runtime.callFunctionOn", {
            "executionContextId": self.world,
            "functionDeclaration": act.document_function(script),
            "arguments": [{"value": arg} for arg in (origin, self.world_key, *args)],
            "returnByValue": True,
        })
        self.assertNotIn("exceptionDetails", outcome)
        return outcome["result"]["value"]

    def describe(self, number):
        return self.run_act(act.DESCRIBE, number)

    def click(self, number):
        return self.run_act(act.CLICK, number, self.describe(number)["binding"])

    def fill(self, number, text=TYPED, submit=False):
        return self.run_act(act.TYPE, number, self.describe(number)["binding"], text, submit)

    def test_describes_kinds_and_clicks_ordinary_targets(self):
        self.load("""
            <a href="/next">Next</a>
            <form action="/post" method="post"><button>Send</button></form>
            <input type="checkbox">
            <input type="radio">
            <button type="button" onclick="window.clicks = (window.clicks || 0) + 1">Control</button>
            <summary role="tab">Tab</summary>
            <input type="search">
            <select><option>Unused</option></select>
            <details><summary>Expand</summary><p>Expanded</p></details>
        """)
        self.assertEqual([self.describe(i)["kind"] for i in range(1, 10)], [
            "link", "submit", "toggle", "toggle", "control", "control", "field", "unusable", "control",
        ])
        self.assertEqual(self.describe(2)["destination_origin"], ORIGIN)
        self.assertEqual(self.describe(2)["method"], "post")
        for number in (3, 4, 5, 9):
            self.assertEqual(self.click(number), "done")
        self.assertTrue(self.page.locator("[type=checkbox]").is_checked())
        self.assertTrue(self.page.locator("[type=radio]").is_checked())
        self.assertEqual(self.page.evaluate("window.clicks"), 1)
        self.assertTrue(self.page.locator("details").evaluate("node => node.open"))

    def test_image_map_areas_are_numbered_links(self):
        result = self.load("""
            <img usemap="#fixture" width="100" height="100" alt="Map">
            <map name="fixture"><area href="/next" shape="rect" coords="0,0,50,50" alt="Next"></map>
        """)
        self.assertEqual([(item["kind"], item["label"]) for item in result["items"]], [("link", "Next")])
        self.assertEqual(self.describe(1)["kind"], "link")
        self.page.locator("img").evaluate("node => node.removeAttribute('usemap')")
        self.assertEqual(self.describe(1)["kind"], "unusable")

    def test_stale_unusable_controls(self):
        self.load("""
            <button id="hide">Hide</button><button id="disable">Disable</button>
            <button id="remove">Remove</button><button id="replace">Replace</button>
            <div id="clip"><button>Clip</button></div><button id="label">Label before</button>
        """)
        self.page.evaluate("""() => {
            document.querySelector('#hide').style.opacity = 0;
            document.querySelector('#disable').disabled = true;
            document.querySelector('#remove').remove();
            const replacement = document.querySelector('#replace');
            replacement.replaceWith(replacement.cloneNode(true));
            document.querySelector('#clip').style.clipPath = 'inset(100%)';
            document.querySelector('#label').textContent = 'Label after';
        }""")
        self.assertEqual([self.describe(i)["kind"] for i in range(1, 7)], ["unusable"] * 6)

    def test_label_and_nested_role_resolve_to_the_native_submitter(self):
        self.load("""
            <label for="send" role="button">Label alias</label>
            <form action="/post" onsubmit="window.submissions++; event.preventDefault()">
                <button id="send" aria-label="Native submit">
                    <span role="button">Nested alias</span>
                </button>
            </form>
            <label for="missing" role="button">Missing target</label>
            <script>window.submissions = 0</script>
        """)
        for number in (1, 2, 3):
            described = self.describe(number)
            self.assertEqual((described["kind"], described["target_number"]), ("submit", 2))
            self.assertEqual(described["destination_origin"], ORIGIN)
        self.assertEqual(self.describe(4), {"kind": "unusable"})
        for alias in (1, 3):
            self.assertEqual(self.click(alias), "done")
        self.assertEqual(self.page.evaluate("window.submissions"), 2)

    def test_label_retargeting_invalidates_the_approved_binding(self):
        self.load("""
            <label for="first" role="button">Alias</label>
            <form onsubmit="window.submitted = true; event.preventDefault()">
                <button id="first">First</button><button id="second">Second</button>
            </form>
        """)
        binding = self.describe(1)["binding"]
        self.page.locator("label").evaluate("node => node.htmlFor = 'second'")
        self.assertEqual(self.run_act(act.CLICK, 1, binding), "changed")
        self.assertEqual(self.describe(1), {"kind": "unusable"})
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_original_bindings_refuse_read_metadata_changes_in_both_scripts(self):
        for attribute in ("type", "autocomplete", "form", "label", "name", "readonly", "disabled"):
            with self.subTest(attribute=attribute):
                self.load("""
                    <form id="first"></form><form id="second"></form>
                    <label for="field">Field before</label>
                    <input id="field" form="first" name="query">
                    <label for="button">Button before</label>
                    <button id="button" type="button" form="first" name="chosen">Press</button>
                    <script>
                    window.events = [];
                    for (const type of ['click', 'input', 'change'])
                        document.addEventListener(type, event => events.push(event.type));
                    </script>
                """)
                field_binding = self.describe(1)["binding"]
                button_binding = self.describe(2)["binding"]
                self.page.evaluate("""attribute => {
                    const field = document.querySelector('#field');
                    const button = document.querySelector('#button');
                    if (attribute === 'label') {
                        field.labels[0].textContent = 'Field after';
                        button.labels[0].textContent = 'Button after';
                        return;
                    }
                    if (attribute === 'type') {
                        field.type = 'search';
                        button.type = 'reset';
                        return;
                    }
                    const value = {form: 'second', autocomplete: 'off', name: 'updated'}[attribute] || '';
                    field.setAttribute(attribute, value);
                    button.setAttribute(attribute, value);
                }""", attribute)
                self.assertEqual(self.run_act(act.TYPE, 1, field_binding, TYPED, False), "changed")
                self.assertEqual(self.run_act(act.CLICK, 2, button_binding), "changed")
                self.assertEqual(self.describe(1), {"kind": "unusable"})
                self.assertEqual(self.describe(2), {"kind": "unusable"})
                self.assertEqual(self.page.locator("#field").input_value(), "")
                self.assertEqual(self.page.evaluate("window.events"), [])

    def test_acting_scripts_check_the_approved_binding_after_destination_changes(self):
        self.load("""
            <form action="/first" onsubmit="events.push(event.type); event.preventDefault()">
                <input name="query" oninput="events.push(event.type)"><button>Send</button>
            </form>
            <script>window.events = []</script>
        """)
        field_binding = self.describe(1)["binding"]
        button_binding = self.describe(2)["binding"]
        self.page.locator("form").evaluate("node => node.action = '/second'")
        # Run immediately with the old bindings, before another describe updates them.
        self.assertEqual(self.run_act(act.TYPE, 1, field_binding, TYPED, True), "changed")
        self.assertEqual(self.run_act(act.CLICK, 2, button_binding), "changed")
        self.assertNotEqual(self.describe(1)["binding"], field_binding)
        self.assertNotEqual(self.describe(2)["binding"], button_binding)
        # A fresh description must not authorize an old caller either.
        self.assertEqual(self.run_act(act.CLICK, 2, button_binding), "changed")
        self.assertEqual(self.run_act(act.TYPE, 1, field_binding, TYPED, True), "changed")
        self.assertEqual(self.page.locator("input").input_value(), "")
        self.assertEqual(self.page.evaluate("window.events"), [])

    def test_sensitive_rules_leave_all_fields_and_events_untouched(self):
        attributes = ['type="password"', 'autocomplete="section-a one-time-code"',
                      'autocomplete="current-password"', 'autocomplete="new-password"',
                      'autocomplete="cc-exp"', 'autocomplete="section-a billing cc-number"', 'readonly', 'id="PWD-hint"', 'id="compass"',
                      'name="OtpEntry"', 'name="verification"', 'autocomplete="CVV"',
                      'id="cvc"', 'name="card-number"', 'id="iban"', 'autocomplete="ssn"',
                      'name="secret-answer"', 'id="token"']
        self.load("".join(f"<input {attrs}>" for attrs in attributes) + """
            <script>
            window.events = [];
            for (const type of ['input','change']) document.addEventListener(type, e => events.push(e.type));
            </script>
        """)
        for number in range(1, len(attributes) + 1):
            with self.subTest(attributes=attributes[number - 1]):
                self.assertTrue(self.describe(number)["sensitive"])
                self.assertEqual(self.fill(number), "changed")
        self.assertEqual(self.page.locator("input").evaluate_all("nodes => nodes.map(n => n.value)"),
                         [""] * len(attributes))
        self.assertEqual(self.page.evaluate("window.events"), [])

    def test_native_setter_and_bubbling_events(self):
        self.load("""
            <textarea></textarea>
            <script>
            const field = document.querySelector('textarea');
            const native = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value');
            Object.defineProperty(field, 'value', {get: native.get, set() { throw Error('framework setter'); }});
            window.events = [];
            for (const type of ['input','change']) document.addEventListener(type, e => events.push([type, e.target.value]));
            </script>
        """)
        typed = TYPED + "\nsecond line"
        self.assertEqual(self.fill(1, typed), "done")
        self.assertEqual(self.page.locator("textarea").input_value(), typed)
        self.assertEqual(self.page.evaluate("window.events"), [["input", typed], ["change", typed]])

    def test_type_and_submit_posts_to_visible_submitters_destination(self):
        target = OTHER + "destination"
        self.fixtures[target] = "<p>Received</p>"
        self.load(f"""
            <form method="post" action="/default">
                <button name="intent" value="before">Before</button>
                <input name="query">
                <button hidden name="intent" value="hidden">Hidden</button>
                <button disabled name="intent" value="disabled">Disabled</button>
                <button formaction="{target}" name="intent" value="visible">Visible after</button>
            </form>
        """)
        self.assertEqual(self.describe(2)["destination_origin"], OTHER.rstrip("/"))
        with self.page.expect_request(target) as request:
            self.assertEqual(self.fill(2, submit=True), "done")
        self.page.wait_for_url(target)
        self.assertEqual(request.value.method, "POST")
        self.assertEqual(request.value.post_data, "query=the+exact+entered+value&intent=visible")

    def test_click_submit_honours_its_native_overrides(self):
        target = OTHER + "destination"
        self.fixtures[target] = "<p>Received</p>"
        self.load(f"""
            <form action="/default" id="fixture"><input name="query" value="example"></form>
            <button form="fixture" formaction="{target}" formmethod="post" name="intent" value="apply">Send</button>
        """)
        with self.page.expect_request(target) as request:
            self.assertEqual(self.click(2), "done")
        self.page.wait_for_url(target)
        self.assertEqual(request.value.method, "POST")
        self.assertEqual(request.value.post_data, "query=example&intent=apply")

    def test_password_form_refuses_submit_but_allows_filling_an_ordinary_field(self):
        self.load("""
            <form onsubmit="window.submitted = true; return false">
                <input name="query"><button>Sign in</button>
            </form>
            <input type="password" form="login">
            <script>document.querySelector('form').id = 'login'</script>
        """)
        self.assertTrue(self.describe(1)["form_has_password"])
        self.assertTrue(self.describe(2)["form_has_password"])
        self.assertEqual(self.fill(1, submit=True), "changed")
        self.assertEqual(self.click(2), "changed")
        self.assertEqual(self.page.locator("[name=query]").input_value(), "")
        self.assertEqual(self.fill(1), "done")
        self.assertEqual(self.page.locator("[name=query]").input_value(), TYPED)
        self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_payment_field_applies_to_form_controls(self):
        self.load("""
            <form id="checkout"><input name="city"><button>Pay</button>
            <input type="checkbox"><span role="button">Choose delivery</span></form>
            <input autocomplete="cc-number" form="checkout">
        """)
        for number in range(1, 6):
            self.assertTrue(self.describe(number)["payment"])
        self.assertFalse(self.describe(1)["sensitive"])
        self.assertTrue(self.describe(5)["sensitive"])

    def test_hidden_password_autocomplete_blocks_submit_through_both_aliases(self):
        self.load("""
            <label role="button" for="send">Alias</label>
            <form onsubmit="window.submitted = true; event.preventDefault()">
                <input type="text" autocomplete="section-a current-password" hidden>
                <input name="query">
                <button id="send"><span role="button">Nested</span></button>
            </form>
        """)
        for number in (1, 3, 4):
            described = self.describe(number)
            self.assertEqual(described["kind"], "submit")
            self.assertTrue(described["form_has_password"])
            self.assertEqual(self.click(number), "changed")
        self.assertTrue(self.describe(2)["form_has_password"])
        self.assertEqual(self.fill(2, submit=True), "changed")
        self.assertIsNone(self.page.evaluate("window.submitted"))
        self.assertEqual(self.page.locator("[name=query]").input_value(), "")

    def test_label_and_placeholder_identify_payment_fields(self):
        self.load("""
            <form><label for="first">Card number</label><input id="first">
                <input placeholder="Security code">
                <label for="third">Expiry</label><input id="third" placeholder="Credit card">
                <input placeholder="Expiration date">
                <input placeholder="IBAN"><button>Pay</button>
            </form>
        """)
        for number in (1, 2, 3, 5):
            with self.subTest(number=number):
                self.assertTrue(self.describe(number)["sensitive"])
                self.assertEqual(self.fill(number), "changed")
        self.assertFalse(self.describe(4)["sensitive"])
        self.assertTrue(self.describe(6)["payment"])
        self.assertEqual(self.page.locator("input").evaluate_all("nodes => nodes.map(n => n.value)"), [""] * 5)

    def test_focus_and_events_cannot_swap_the_approved_operation(self):
        self.load("""
            <form method="post" action="/first">
                <input id="focus" onfocus="this.type = 'password'">
                <input id="event" oninput="this.form.action = '/second'">
                <button>Submit</button>
            </form>
        """)
        self.assertEqual(self.fill(1), "changed")
        self.assertEqual(self.page.locator("#focus").input_value(), "")
        self.page.locator("#focus").evaluate("node => node.type = 'text'")
        self.assertEqual(self.fill(2, submit=True), "filled")
        self.assertEqual(self.page.url, PAGE)
        self.assertEqual(self.page.locator("#event").input_value(), TYPED)

    def test_destination_origin_includes_port_and_scheme_and_is_rechecked_after_fill(self):
        for origin in ("http://example.invalid:8080", "https://example.invalid"):
            with self.subTest(origin=origin):
                self.load(f"""
                    <form action="{origin}/post" onsubmit="window.submitted = true; event.preventDefault()">
                        <input oninput="this.form.action = '/post'"><button>Send</button>
                    </form>
                """)
                self.assertEqual(self.describe(1)["destination_origin"], origin)
                self.assertEqual(self.describe(2)["destination_origin"], origin)
                self.assertEqual(self.fill(1, submit=True), "filled")
                self.assertEqual(self.page.locator("input").input_value(), TYPED)
                self.assertIsNone(self.page.evaluate("window.submitted"))

    def test_enter_in_form_without_submitter_and_refusal_outside_form(self):
        self.load("""
            <form><input onkeydown="if (event.key === 'Enter') window.enter = this.value"></form>
            <div><input><button type="button" onclick="window.nearby = this.previousElementSibling.value">Next</button></div>
        """)
        self.assertEqual(self.fill(1, submit=True), "done")
        self.assertFalse(self.describe(2)["can_submit"])
        self.assertEqual(self.fill(2, submit=True), "changed")
        self.assertEqual(self.page.locator("div input").input_value(), "")
        self.assertEqual(self.page.evaluate("[window.enter, window.nearby]"), [TYPED, None])

    def test_origin_top_frame_and_replacement(self):
        self.load("<button>Original</button>")
        self.assertEqual(self.run_act(act.DESCRIBE, 1, origin=OTHER.rstrip("/")), "moved")
        self.fixtures[OTHER] = "<button>New document</button>"
        self.page.goto(OTHER)
        old_context = self.world
        from playwright.sync_api import Error

        with self.assertRaisesRegex(Error, "(?i)context"):
            self.run_act(act.DESCRIBE, 1)
        self.document_read(OTHER.rstrip("/"))
        self.assertNotEqual(self.world, old_context)
        self.assertEqual(self.run_act(act.DESCRIBE, 1), "moved")
        self.fixtures[PAGE + "frame"] = "<button>Child</button>"
        self.load('<iframe src="/frame"></iframe>')
        frame = self.tree()["childFrames"][0]["frame"]["id"]
        self.assertEqual(self.document_read(frame=frame), "moved")
        self.assertEqual(self.run_act(act.DESCRIBE, 1), "moved")

    def test_window_open_does_not_replace_numbered_tab(self):
        self.fixtures[OTHER] = "<p>Popup fixture</p>"
        self.load(f"""<button type="button" onclick="window.open('{OTHER}')">Open popup</button>""")
        before = self.location()
        with self.page.expect_popup() as event:
            self.assertEqual(self.click(1), "done")
        event.value.close()
        self.assertEqual(self.location(), before)
        self.assertEqual(self.describe(1)["kind"], "control")


if __name__ == "__main__":
    unittest.main(verbosity=2)
