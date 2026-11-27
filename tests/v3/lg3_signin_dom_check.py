"""Run the fixed sign-in scripts against route-fulfilled Chromium fixtures."""

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from lg1_login_store_check import MemoryBackend, NAME, ORIGIN, TYPED, USER

from jarvis.v3.browser import signin
from jarvis.v3.browser.session import Landing, Location
from jarvis.v3.logins import Store


PAGE = ORIGIN + "/"
OTHER = "https://other.example.invalid/"
FORM = """
<form id="login" method="post">
  <input id="who" autocomplete="username">
  <input id="key" type="password">
  <button>Sign in</button>
</form>
<script>
  login.addEventListener('submit', event => {
    event.preventDefault();
    window.received = [who.value, key.value];
  });
</script>
"""
TRACK_KEY_EVENTS = """
<script>
  window.originalKey = key;
  window.keyEvents = [];
  for (const type of ['input', 'change']) {
    originalKey.addEventListener(type, () => keyEvents.push(type));
  }
</script>
"""


class SignInDOM(unittest.TestCase):
    def setUp(self):
        cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
        home = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "HOME": home, "XDG_CACHE_HOME": home, "XDG_CONFIG_HOME": home,
            "XDG_DATA_HOME": home, "XDG_RUNTIME_DIR": home, "PLAYWRIGHT_BROWSERS_PATH": cache,
        }))
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            self.skipTest("Playwright is not installed.")
        self.error = Error
        playwright = self.enterContext(sync_playwright())
        try:
            browser = playwright.chromium.launch(headless=True, timeout=15_000, args=[
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--disable-background-networking",
            ])
        except Error as exc:
            if any(reason in str(exc) for reason in ("Executable doesn't exist",
                    "Host system is missing dependencies", "error while loading shared libraries")):
                self.skipTest("Playwright Chromium or its system dependencies are unavailable.")
            raise
        self.addCleanup(browser.close)
        context = browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.fixtures = {}
        self.headers = {}
        self.requests = []
        context.route("**/*", self.route)
        self.cdp = context.new_cdp_session(self.page)

    def route(self, route):
        request = route.request
        self.requests.append(request)
        if request.url in self.fixtures:
            route.fulfill(content_type="text/html; charset=utf-8", headers=self.headers.get(request.url),
                          body="<!doctype html>" + self.fixtures[request.url])
        else:
            route.abort()

    def load(self, html, url=PAGE, *, headers=None):
        self.fixtures[url] = html
        self.headers[url] = headers
        self.page.goto(url)

    def world(self, *, child=False):
        tree = self.cdp.send("Page.getFrameTree")["frameTree"]
        if child:
            tree = tree["childFrames"][0]
        return self.cdp.send("Page.createIsolatedWorld", {
            "frameId": tree["frame"]["id"],
        })["executionContextId"]

    def call_script(self, script, *args, world=None, origin=ORIGIN):
        response = self.cdp.send("Runtime.callFunctionOn", {
            "executionContextId": self.world() if world is None else world,
            "functionDeclaration": signin.document_function(script),
            "arguments": [{"value": value} for value in (origin, *args)],
            "returnByValue": True,
        })
        self.assertNotIn("exceptionDetails", response)
        result = response["result"]["value"]
        if isinstance(result, dict):
            key = "signature" if script is signin.INSPECT else "before"
            self.assertEqual(set(result), {"result", key})
            self.assertIsInstance(result[key], list)
            self.assertEqual(len(result[key]), 2)
            self.assertTrue(all(type(count) is int and count >= 0 for count in result[key]))
        return result

    def run_script(self, script, *args, **kwargs):
        result = self.call_script(script, *args, **kwargs)
        return result["result"] if isinstance(result, dict) else result

    def assert_filled(self):
        self.assertEqual(self.page.evaluate("window.received"), [USER, TYPED])

    def location(self, *, timeout=None):
        frame = self.cdp.send("Page.getFrameTree")["frameTree"]["frame"]
        return Location(frame["url"] + frame.get("urlFragment", ""), frame["id"], frame["loaderId"])

    def run_sign_in(self, html, expected):
        store = Store(MemoryBackend())
        store.add(NAME, ORIGIN, USER, TYPED)
        actions = []
        inspections = []

        def open_page(url):
            self.load(html, url)
            return Landing(url, "loaded")

        def document_call(origin, script, arguments):
            result = self.call_script(script, *(arguments or ()), origin=origin)
            if script is signin.ACT:
                actions.append(result)
            else:
                inspections.append(result)
            return result

        session = SimpleNamespace(open=open_page, location=self.location, document_call=document_call)
        self.assertEqual(signin.sign_in(session, store, NAME).result, expected)
        action, = actions
        self.assertEqual(action["result"], "handled")
        return action["before"], inspections[-1]["signature"]

    def assert_key_untouched(self):
        self.assertEqual(self.page.evaluate("[originalKey.value, keyEvents]"), ["", []])

    def assert_native_submission_blocked(self, before):
        self.assertEqual(self.page.locator("#key").input_value(), TYPED)
        self.assertEqual(self.location(), before)
        self.assertEqual(self.page.url, PAGE)
        self.assertEqual([request.url for request in self.requests], [PAGE])
        self.assertEqual(self.page.context.pages, [self.page])

    def test_plain_form_including_native_request_submit(self):
        self.load(FORM.replace('<button>', '<button name="requestSubmit">'))
        self.assertEqual(self.run_script(signin.INSPECT), "both")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assert_filled()

    def test_two_documents(self):
        next_page = PAGE + "enter"
        self.fixtures[next_page] = """
            <form id="login" method="post"><input id="key" type="password"><button>Log in</button></form>
            <script>
              login.onsubmit = event => {
                event.preventDefault();
                window.received = [sessionStorage.getItem('fixture-user'), key.value];
              };
            </script>
        """
        self.load("""
            <form id="login"><input id="who" type="email"><button>Next</button></form>
            <script>
              login.onsubmit = event => {
                event.preventDefault();
                sessionStorage.setItem('fixture-user', who.value);
                location.assign('/enter');
              };
            </script>
        """)
        self.assertEqual(self.run_script(signin.INSPECT), "username")
        self.assertEqual(self.run_script(signin.ACT, "username", USER), "handled")
        self.page.wait_for_url(next_page)
        self.assertEqual(self.run_script(signin.INSPECT), "password")
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.assert_filled()

    def test_controlled_fields_need_native_setter_and_bubbling_events(self):
        self.load(FORM + """
            <script>
              window.received = null;
              let rendered = {};
              let changed = [];
              for (const field of [who, key]) {
                Object.defineProperty(field, 'value', {set() {}, get() {return rendered[field.id];}});
              }
              const native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').get;
              login.addEventListener('input', event => {
                rendered[event.target.id] = native.call(event.target);
              });
              login.addEventListener('change', event => changed.push(event.target.id));
              login.onsubmit = () => window.changed = changed;
              who.value = 'ignored';
            </script>
        """)
        self.assertIsNone(self.page.evaluate("who.value"))
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assert_filled()
        self.assertEqual(self.page.evaluate("window.changed"), ["who", "key"])

    def test_hidden_and_decoy_fields(self):
        decoys = """
            <input type="password" id="off_form">
            <form><input type="password" style="display:none"></form>
            <form style="opacity:0"><input type="password"></form>
            <form style="visibility:hidden"><input type="password"></form>
            <form aria-hidden="true"><input type="password"></form>
            <details><form><input type="password"></form></details>
            <form inert><input type="password"></form>
            <form><input type="password" disabled></form>
            <form><input type="password" readonly></form>
            <input type="email" id="outside_email">
        """
        form = FORM.replace('<input id="who" autocomplete="username">', """
            <input id="earlier" autocomplete="username">
            <input id="who" autocomplete="username">
            <input id="email" name="email" type="text">
            <div aria-hidden="true"><input autocomplete="username"></div>
        """)
        self.load(decoys + form)
        self.assertEqual(self.run_script(signin.INSPECT), "both")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assert_filled()
        self.assertEqual(self.page.locator("input:not(#who, #key)").evaluate_all(
            "inputs => inputs.map(input => input.value)"), [""] * 13)

    def test_inner_frame_is_not_filled_even_with_same_origin(self):
        self.fixtures[PAGE + "child"] = FORM
        self.load('<iframe src="/child"></iframe>')
        self.assertEqual(self.run_script(signin.INSPECT), "none")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "failed")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED, world=self.world(child=True)), "moved")
        self.assertEqual(self.page.frame_locator("iframe").locator("#key").input_value(), "")

    def test_wrong_origin_does_not_touch_any_field(self):
        self.load(FORM, OTHER)
        self.assertEqual(self.run_script(signin.INSPECT), "moved")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
        self.assertEqual(self.page.locator("#who").input_value(), "")
        self.assertEqual(self.page.locator("#key").input_value(), "")
        self.assertIsNone(self.page.evaluate("window.received"))

    def test_top_level_csp_sandbox_has_an_opaque_origin(self):
        self.load(FORM, headers={"Content-Security-Policy": "sandbox allow-scripts"})
        self.assertEqual(self.page.evaluate("[location.origin, self.origin]"), [ORIGIN, "null"])
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
        self.assertEqual(self.page.locator("#who").input_value(), "")
        self.assertEqual(self.page.locator("#key").input_value(), "")

    def test_blob_document_is_refused_by_the_wrapper(self):
        self.load(FORM)
        blob = self.page.evaluate("(html) => URL.createObjectURL(new Blob([html], {type:'text/html'}))", FORM)
        self.page.goto(blob)
        self.assertEqual(self.page.evaluate("[location.protocol, location.origin, self.origin]"),
                         ["blob:", ORIGIN, ORIGIN])
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
        self.assertEqual(self.page.locator("#who").input_value(), "")
        self.assertEqual(self.page.locator("#key").input_value(), "")

    def test_relaxed_document_domain_is_refused(self):
        self.load(FORM, headers={"Origin-Agent-Cluster": "?0"})
        self.page.evaluate("document.domain = 'example.invalid'")
        self.assertEqual(self.page.evaluate("document.domain"), "example.invalid")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
        self.assertEqual(self.page.locator("#key").input_value(), "")

    def test_blocks_unsafe_native_submissions_after_filling(self):
        for form, button, base in (
            (f'method="post" action="{OTHER}"', '', ''),
            ('method="post" action="/finish"', f'formaction="{OTHER}"', ''),
            ('method="post" action="finish"', '', f'<base href="{OTHER}">'),
            ('method="get" action="/finish"', '', ''),
            ('action="/finish"', '', ''),
            ('method="post" action="/finish"', 'formmethod="get"', ''),
            ('method="post" action="/finish" target="_blank"', '', ''),
        ):
            with self.subTest(form=form, button=button, base=base):
                self.requests.clear()
                self.load(base + f"""
                    <form {form}>
                      <input id="who" name="who" autocomplete="username">
                      <input id="key" name="key" type="password">
                      <button {button}>Sign in</button>
                    </form>
                """)
                before = self.location()
                self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "handled")
                self.assert_native_submission_blocked(before)
                self.assertEqual(self.page.locator("#who").input_value(), USER)

    def test_named_frame_and_overridden_targets(self):
        destination = PAGE + "finish"
        self.fixtures[destination] = "<p>Done</p>"
        self.load("""
            <base target="other">
            <iframe name="other" srcdoc="<p>Waiting</p>"></iframe>
            <form id="login" method="post" action="/finish" target="_self">
              <input id="key" name="key" type="password">
              <button id="submitter" formtarget="other">Sign in</button>
            </form>
        """)
        before = self.location()
        for target in ("other", ""):
            with self.subTest(target=target):
                self.page.evaluate("target => submitter.setAttribute('formtarget', target)", target)
                self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "handled")
                self.assert_native_submission_blocked(before)
                self.assertEqual(self.page.frame(name="other").url, "about:srcdoc")

        # Ask Chromium directly where the empty override goes after the guard is removed.
        with self.page.expect_request_finished(lambda request: request.method == "POST") as posted:
            self.page.evaluate("login.requestSubmit(submitter)")
        self.assertEqual(posted.value.frame.name, "other")
        self.assertEqual(posted.value.url, destination)
        self.assertEqual(self.location(), before)

        self.page.evaluate("submitter.setAttribute('formtarget', '_SELF')")
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.page.wait_for_url(destination)
        self.assertEqual((self.requests[-1].method, self.requests[-1].url), ("POST", destination))

    def test_a_base_element_in_another_namespace_sets_no_target(self):
        # Only an HTML base element sets the target. One inside svg must not
        # make a same-tab post look like a post into another frame.
        destination = PAGE + "finish"
        self.fixtures[destination] = "<p>Done</p>"
        self.load("""
            <svg><base target="other"></base></svg>
            <form method="post" action="/finish">
              <input name="key" type="password">
              <button>Sign in</button>
            </form>
        """)
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.page.wait_for_url(destination)
        self.assertEqual((self.requests[-1].method, self.requests[-1].url), ("POST", destination))

    def test_a_non_https_action_is_never_submitted_natively(self):
        self.load("""
            <form id="login" method="post">
              <input id="key" name="key" type="password">
              <button>Sign in</button>
            </form>
        """)
        self.page.evaluate("login.action = URL.createObjectURL(new Blob(['x'], {type: 'text/html'}))")
        before = self.location()
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "handled")
        self.assert_native_submission_blocked(before)

    def test_script_handler_can_post_without_native_navigation(self):
        for attrs in ("", f'action="{OTHER}" method="post"'):
            with self.subTest(attrs=attrs):
                self.requests.clear()
                self.fixtures[PAGE + "finish"] = "<p>OK</p>"
                self.run_sign_in(f"""
                    <form id="login" {attrs}>
                      <input id="key" name="key" type="password"><button>Sign in</button>
                    </form>
                    <script>
                      window.originalDocument = document;
                      login.addEventListener('submit', async event => {{
                        event.preventDefault();
                        await Promise.resolve();
                        await fetch('/finish', {{method: 'POST', body: new URLSearchParams({{key: key.value}})}});
                        login.replaceWith(document.createTextNode('Signed in'));
                      }});
                    </script>
                """, "signed_in")
                self.assertTrue(self.page.evaluate("originalDocument === document"))
                self.assertEqual(self.page.url, PAGE)
                self.assertEqual([(r.method, r.url) for r in self.requests],
                                 [("GET", PAGE), ("POST", PAGE + "finish")])
                self.assertEqual(parse_qs(self.requests[-1].post_data), {"key": [TYPED]})

    def test_form_without_handler_reports_wrong_destination(self):
        for attrs in ("", f'action="{OTHER}" method="post"'):
            with self.subTest(attrs=attrs):
                self.requests.clear()
                before, after = self.run_sign_in(f"""
                    <form {attrs}>
                      <input id="key" name="key" type="password"><button>Sign in</button>
                    </form>
                """, "wrong_destination")
                self.assertEqual(before, after)
                self.assertEqual(self.page.locator("#key").input_value(), TYPED)
                self.assertEqual(self.page.url, PAGE)
                self.assertEqual([(r.method, r.url) for r in self.requests], [("GET", PAGE)])

    def test_script_handler_shows_an_error_and_reports_rejected(self):
        before, after = self.run_sign_in("""
            <form id="login">
              <input id="key" type="password"><button>Sign in</button>
            </form>
            <div id="message" role="alert"></div>
            <script>
              login.addEventListener('submit', () => {
                message.textContent = 'Try again';
              });
            </script>
        """, "rejected")
        self.assertNotEqual(before, after)
        self.assertEqual((before[1], after[1]), (0, 1))
        self.assertTrue(self.page.locator("#key").is_visible())

    def test_aria_live_change_counts_even_when_body_text_length_is_equal(self):
        before, after = self.run_sign_in("""
            <form id="login">
              <input id="key" type="password"><button>Sign in</button>
            </form>
            <span id="hint">Try again</span><span id="message" aria-live="polite"></span>
            <div role="alert" hidden>Hidden message</div>
            <div aria-invalid="true" style="opacity: 0">Transparent message</div>
            <script>
              login.addEventListener('submit', () => {
                message.textContent = hint.textContent;
                hint.textContent = '';
              });
            </script>
        """, "rejected")
        self.assertEqual(before[0], after[0])
        self.assertEqual((before[1], after[1]), (0, 1))

    def test_document_capture_cannot_stop_window_guard(self):
        self.load("""
            <form><input id="key" name="key" type="password"><button>Sign in</button></form>
            <script>
              document.addEventListener('submit', event => {
                window.cancelledAtDocument = event.defaultPrevented;
                event.stopPropagation();
              }, true);
            </script>
        """)
        before = self.location()
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "handled")
        self.assertTrue(self.page.evaluate("window.cancelledAtDocument"))
        self.assert_native_submission_blocked(before)

    def test_rechecks_destination_after_password_focus(self):
        self.load(FORM.replace('<input id="key" type="password">',
                               """<input id="key" type="password" onfocus="login.method='get'">""")
                  + TRACK_KEY_EVENTS)
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
        self.assert_key_untouched()
        self.assertIsNone(self.page.evaluate("window.received"))
        self.assertEqual([request.url for request in self.requests], [PAGE])

    def test_guards_native_submission_if_destination_changes_after_fill(self):
        self.load(FORM + f"""
            <script>
              window.events = [];
              for (const type of ['input', 'change']) {{
                key.addEventListener(type, () => events.push([type, key.value]));
              }}
              key.addEventListener('change', () => login.action = '{OTHER}');
            </script>
        """)
        before = self.location()
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "handled")
        self.assert_native_submission_blocked(before)
        self.assert_filled()
        self.assertEqual(self.page.evaluate("window.events"), [
            ["input", TYPED], ["change", TYPED],
        ])

    def test_password_can_become_readonly_after_fill_without_blocking_safe_submission(self):
        self.load(FORM.replace('type="password"', 'type="password" onchange="this.readOnly=true"'))
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assert_filled()

    def test_posts_to_absolute_same_origin_action_without_a_submit_control(self):
        destination = PAGE + "finish"
        self.fixtures[destination] = "<p>Done</p>"
        self.load(f"""
            <form method="post" action="{destination}">
              <input name="action" id="key" type="password">
              <input name="method" type="hidden">
              <input name="target" type="hidden" disabled>
              <input name="getAttribute" type="hidden" disabled>
            </form>
        """)
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.page.wait_for_url(destination)
        request = self.requests[-1]
        self.assertEqual((request.method, request.url), ("POST", destination))
        self.assertEqual(parse_qs(request.post_data), {"action": [TYPED]})

    def test_submitter_after_password_supplies_destination_method_and_intent(self):
        destination = PAGE + "finish"
        self.fixtures[destination] = "<p>Done</p>"
        self.load(f"""
            <form id="login" method="get" action="{OTHER}">
              <button>Earlier</button>
              <input name="key" type="password">
              <button hidden>Hidden</button>
              <fieldset disabled><button>Disabled</button></fieldset>
            </form>
            <button form="login" formaction="/finish" formmethod="POST"
                    name="intent" value="login">Sign in</button>
            <button form="login">Later</button>
        """)
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.page.wait_for_url(destination)
        request = self.requests[-1]
        self.assertEqual((request.method, request.url), ("POST", destination))
        self.assertEqual(parse_qs(request.post_data), {"key": [TYPED], "intent": ["login"]})

    def test_empty_action_posts_to_document_even_with_another_base(self):
        for action, override in (('action=""', ''), (f'action="{OTHER}"', 'formaction=""')):
            with self.subTest(action=action, override=override):
                self.load(f"""
                    <base href="{OTHER}">
                    <form method="post" {action}>
                      <input name="key" type="password">
                      <button {override}>Sign in</button>
                    </form>
                """)
                with self.page.expect_request_finished(lambda request: request.method == "POST") as posted:
                    self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
                request = posted.value
                self.assertEqual(request.url, PAGE)
                self.assertEqual(parse_qs(request.post_data), {"key": [TYPED]})

    def test_submitter_before_password_is_used_when_there_is_none_after(self):
        self.load(FORM.replace('<button>Sign in</button>', '').replace(
            '<input id="key"', '<input type="image"> <input id="key"'
        ).replace("window.received = [who.value, key.value];",
                  "window.received = event.submitter.type;"))
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assertEqual(self.page.evaluate("window.received"), "image")

    def test_hidden_only_submitter_does_not_trigger_submission_or_fallback(self):
        self.load(FORM.replace('<button>', '<button hidden>'))
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "failed")
        self.assertEqual(self.page.locator("#key").input_value(), "")
        self.assertIsNone(self.page.evaluate("window.received"))

    def test_focus_handler_cannot_change_input_type_before_the_fill(self):
        for field, initial, changed in (("key", "password", "text"), ("who", "text", "search")):
            with self.subTest(field=field):
                self.load(FORM.replace(f'id="{field}"',
                                       f'''id="{field}" onfocus="this.type='{changed}'"''') + TRACK_KEY_EVENTS)
                self.assertEqual(self.page.locator(f"#{field}").evaluate("field => field.type"), initial)
                self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
                self.assertEqual(self.page.locator(f"#{field}").evaluate("field => field.type"), changed)
                self.assertEqual(self.page.locator(f"#{field}").input_value(), "")
                self.assert_key_untouched()
                self.assertIsNone(self.page.evaluate("window.received"))

    def test_username_input_cannot_remove_or_reassociate_password_before_fill(self):
        for change in ("originalKey.remove()", "otherForm.append(originalKey)"):
            with self.subTest(change=change):
                self.load(FORM + TRACK_KEY_EVENTS + f"""
                    <form id="otherForm" method="post"></form>
                    <script>
                      who.oninput = () => {change};
                    </script>
                """)
                self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "moved")
                self.assert_key_untouched()
                self.assertIsNone(self.page.evaluate("window.received"))

    def test_replaced_document_cannot_receive_call_for_old_world(self):
        self.load(FORM)
        world = self.world()
        self.load(FORM, PAGE + "replacement")
        with self.assertRaisesRegex(self.error, "[Cc]ontext"):
            self.run_script(signin.ACT, "both", USER, TYPED, world=world)
        self.assertEqual(self.page.locator("#who").input_value(), "")
        self.assertEqual(self.page.locator("#key").input_value(), "")
        self.assertIsNone(self.page.evaluate("window.received"))

    def test_closest_button_without_form(self):
        self.load("""
            <button type="button" onclick="window.received = 'wrong'">Continue</button>
            <section>
              <input name="account" id="who" type="tel"><input id="key" type="password">
              <button type="button" onclick="window.received = [who.value, key.value]">Sign in</button>
            </section>
        """)
        self.assertEqual(self.run_script(signin.INSPECT), "both")
        self.assertEqual(self.run_script(signin.ACT, "both", USER, TYPED), "submitted")
        self.assert_filled()

    def test_enter_when_there_is_no_form_or_button(self):
        self.load("""
            <input id="key" type="password" onkeydown="if (event.key === 'Enter') window.received = key.value">
        """)
        self.assertEqual(self.run_script(signin.ACT, "password", "", TYPED), "submitted")
        self.assertEqual(self.page.evaluate("window.received"), TYPED)

    def test_code_field_and_after_submit_visible_text(self):
        for html, initial, after in (
            ('<input autocomplete="one-time-code">', "code", "code"),
            ('<p>Enter the code from your authenticator</p>', "none", "code"),
            ('<p hidden>Verification code</p>', "none", "none"),
            ('<input type="password"><p>Verification code</p>', "password", "password"),
        ):
            with self.subTest(initial=initial, after=after):
                self.load(html)
                self.assertEqual(self.run_script(signin.INSPECT), initial)
                self.assertEqual(self.run_script(signin.INSPECT, "after"), after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
