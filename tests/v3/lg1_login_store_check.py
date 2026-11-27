"""Saved login checks with no real Secret Service connection."""

from contextlib import redirect_stderr, redirect_stdout
import getpass
import io
from pathlib import Path
import subprocess
import sys
import traceback
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
import warnings

from jarvis.v3 import cli
from jarvis.v3.logins import Login, LoginError, Store, canonical_origin


NAME = "example"
ORIGIN = "https://accounts.example.invalid"
USER = "user@example.com"
# Made-up values. Named and assembled so that a secret scanner does not read
# them as real credentials.
TYPED = " " + "fake-pass" + "word-é "
TYPED_NEW = "-".join(("fake", "new", "pass" + "word"))
ATTRIBUTES = dict(application="jarvis-login", schema="1", name=NAME,
                  origin=ORIGIN, username=USER, browser="chrome")


class MemoryBackend:
    def __init__(self):
        self.entries = {}
        self.next_handle = 0
        self.reads = []
        self.deletes = []

    def items(self):
        return [(handle, attrs.copy()) for handle, (attrs, _) in self.entries.items()]

    def put(self, attributes, secret, label):
        self.entries[self.next_handle] = (attributes.copy(), secret)
        self.next_handle += 1

    def secret(self, handle):
        self.reads.append(handle)
        return self.entries[handle][1]

    def delete(self, handle):
        self.deletes.append(handle)
        del self.entries[handle]


class LoginCheck(unittest.TestCase):
    def safe_error(self, operation, expected=None):
        with self.assertRaises(LoginError) as caught:
            operation()
        if expected is not None:
            self.assertEqual(str(caught.exception), expected)
        for rendered in (str(caught.exception), repr(caught.exception)):
            self.assertNotIn(TYPED, rendered)
            self.assertNotIn("fake-password", rendered)
        return caught.exception


class StoreCheck(LoginCheck):
    def setUp(self):
        self.backend = MemoryBackend()
        self.store = Store(self.backend)

    def add(self, **overrides):
        options = dict(name=NAME, site=ORIGIN, username=USER, password=TYPED)
        return self.store.add(**(options | overrides))

    def test_canonical_origin(self):
        cases = [
            ("HTTPS://ACCOUNTS.EXAMPLE.INVALID:443", ORIGIN),
            (ORIGIN + ":0444", ORIGIN + ":444"),
            (ORIGIN + ":65535", ORIGIN + ":65535"),
            (ORIGIN + "/sign-in?next=%20#form", ORIGIN),
            # A Unicode host is refused, never mapped: the browser's own mapping
            # can differ (final sigma), and its ASCII form is compared as it is.
            ("https://bücher.example.invalid", None),
            ("https://faß.example.invalid", None),
            ("https://ΟΣ.0.example.invalid", None),
            ("https://xn--bcher-kva.example.invalid", "https://xn--bcher-kva.example.invalid"),
            ("https://XN--0XAI.0.example.invalid", "https://xn--0xai.0.example.invalid"),
            (ORIGIN + "/caf\u00e9", None),
            ("https://intranet", None),
            ("https://under_score.example.invalid", None),
            ("https://-edge.example.invalid", None),
            ("https://" + "a" * 64 + ".example.invalid", None),
            ("https://" + ".".join(["a" * 60] * 5) + ".invalid", None),
            ("https://user@example.invalid", None),
            ("https://user:" + "fake-pass" + "word@example.invalid", None),
            ("https://@example.invalid", None),
            ("http://example.invalid", None),
            ("example.invalid", None),
            ("https:///example.invalid", None),
            ("https://192.0.2.1", None),
            ("https://[2001:db8::1]", None),
            ("https://[v1.example]", None),
            ("https://0xc0000201", None),
            ("https://3221225985", None),
            ("https://LOCALHOST", None),
            ("https://.", None),
            ("https://example.invalid.", None),
            ("https://example..invalid", None),
            ("https://.example.invalid", None),
            ("https://example。．invalid", None),
            ("https://example.invalid:", None),
            ("https://example.invalid:0", None),
            ("https://example.invalid:65536", None),
            ("https://example.invalid:-1", None),
            ("https://example.invalid:abc", None),
            ("https://%65xample.invalid", None),
            ("https://\ud800.example.invalid", None),
            (ORIGIN + "/page name", None),
            (" " + ORIGIN, None),
            (ORIGIN + "/a\\b", None),
            (ORIGIN + "/\n", None),
            (ORIGIN + "/\x1f", None),
            (ORIGIN + "/\x7f", None),
            (ORIGIN + "/\x85", None),
            (None, None),
            (b"https://example.invalid", None),
            (23, None),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(canonical_origin(source), expected)

    def test_round_trip_and_metadata_reads(self):
        login = self.add(site=ORIGIN.upper() + "/")
        self.add(name="alpha")
        self.assertEqual(login, Login(NAME, ORIGIN, USER, "chrome"))
        listed = self.store.list()
        self.assertEqual([entry.name for entry in listed], ["alpha", NAME])
        self.assertEqual(self.backend.reads, [])
        result = self.store.secret_for(NAME, ORIGIN + "/sign-in?next=home")
        self.assertEqual(result, (login, TYPED))
        self.assertEqual(self.backend.reads, [0])
        for value in (login, listed, result, self.store):
            self.assertNotIn("fake-password", str(value))
            self.assertNotIn("fake-password", repr(value))

    def test_origin_binding_and_replacement(self):
        self.add()
        new_origin = "https://other.example.invalid"
        for site in (new_origin, "https://sub.accounts.example.invalid", "https://example.invalid",
                     ORIGIN + ":444", ORIGIN.replace("https:", "http:"),
                     "https://accоunts.example.invalid", ORIGIN + ".lookalike.invalid",
                     f"https://example.invalid/{TYPED.strip()}" + "x" * 1024):
            with self.subTest(site=site):
                self.safe_error(lambda: self.store.secret_for(NAME, site),
                                "The page is not the site for login example.")
        self.assertEqual(self.backend.reads, [])
        self.assertEqual(self.store.secret_for(NAME, ORIGIN + "/login?next=home")[1], TYPED)
        self.add(site=new_origin, username="another@example.com", password=TYPED_NEW,
                 browser="edge", replace=True)
        self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN))
        self.assertEqual(self.store.secret_for(NAME, new_origin),
                         (Login(NAME, new_origin, "another@example.com", "edge"), TYPED_NEW))
        self.assertEqual({name for name in dir(Store) if not name.startswith("_")},
                         {"system", "add", "list", "remove", "secret_for"})

    def test_unicode_host_cannot_borrow_another_origin(self):
        # Two ASCII origins that one Unicode spelling can map to, depending on the mapper.
        self.add(site="https://xn--0xag.0.example.invalid")
        for page in ("https://ΟΣ.0.example.invalid/", "https://xn--0xai.0.example.invalid/"):
            with self.subTest(page=page):
                self.safe_error(lambda: self.store.secret_for(NAME, page),
                                "The page is not the site for login example.")
        self.assertEqual(self.backend.reads, [])
        self.assertEqual(self.store.secret_for(NAME, "https://xn--0xag.0.example.invalid/x")[1], TYPED)

    def test_failed_replacement_keeps_the_old_login(self):
        self.add()
        new_origin = "https://other.example.invalid"
        with patch.object(self.backend, "put", side_effect=LoginError("write failed")):
            self.safe_error(lambda: self.add(site=new_origin, password=TYPED_NEW, replace=True),
                            "write failed")
        self.assertEqual(self.store.secret_for(NAME, ORIGIN)[1], TYPED)
        self.safe_error(lambda: self.store.secret_for(NAME, new_origin))
        # A failed clean-up leaves two items. Neither can be read until the owner removes them.
        with patch.object(self.backend, "delete", side_effect=LoginError("delete failed")):
            self.safe_error(lambda: self.add(site=new_origin, password=TYPED_NEW, replace=True),
                            "delete failed")
        for page in (ORIGIN, new_origin):
            self.safe_error(lambda: self.store.secret_for(NAME, page), "Expected one saved login named example.")
        self.assertTrue(self.store.remove(NAME))
        self.assertEqual(self.backend.entries, {})

    def test_validation(self):
        bad = {
            "name": ("", "A", "-edge", "edge-", "a" * 33, "a\n", None),
            "site": ("http://example.invalid", ORIGIN + "/login", ORIGIN + "?",
                     ORIGIN + "?x=y", ORIGIN + "#", ORIGIN + "#section", None),
            "username": ("", "a" * 255, "a\x7f", "a\x00", "a\n", None),
            "password": ("", "a" * 1025, TYPED + "\n", TYPED + "\r", TYPED + "\x00", b"fake-password"),
            "browser": ("firefox", "Chrome", None),
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field):
                    self.safe_error(lambda: self.add(**{field: value}))
        self.assertEqual(self.backend.entries, {})
        self.add(name="a" * 32, username="u" * 254, password="p" * 1024)
        self.assertEqual(self.store.secret_for("a" * 32, ORIGIN)[1], "p" * 1024)

    def test_limit_allows_replacing_at_capacity(self):
        for index in range(64):
            self.add(name=f"example-{index}")
        self.safe_error(self.add, "The limit is 64 saved logins.")
        self.add(name="example-0", replace=True)
        self.assertEqual(len(self.store.list()), 64)

    def test_leftovers_of_failed_replacements_do_not_use_up_the_limit(self):
        for index in range(63):
            self.add(name=f"example-{index}")
        with patch.object(self.backend, "delete", side_effect=LoginError("delete failed")):
            for _ in range(3):
                self.safe_error(lambda: self.add(name="example-0", replace=True), "delete failed")
        self.assertEqual(len(self.backend.entries), 66)
        self.add(name="example-63")
        self.safe_error(lambda: self.add(name="example-64"), "The limit is 64 saved logins.")
        self.add(name="example-0", replace=True)
        self.assertEqual(len(self.backend.entries), 64)
        self.assertEqual(len(self.store.list()), 64)

    def test_duplicates_and_removal(self):
        self.add()
        self.backend.put(ATTRIBUTES, "fake-old-password", "")
        self.backend.put(ATTRIBUTES | {"schema": "bad"}, "fake-old-password", "")
        self.safe_error(self.add, "A saved login named example already exists. Use --replace to replace it.")
        self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN))
        self.assertEqual(self.backend.reads, [])
        self.add(replace=True)
        self.assertEqual(self.backend.deletes, [0, 1, 2])
        self.assertEqual(len(self.store.list()), 1)
        self.backend.put(ATTRIBUTES, TYPED, "")
        self.assertTrue(self.store.remove(NAME))
        self.assertFalse(self.store.remove(NAME))
        self.assertEqual(self.backend.entries, {})

    def test_damaged_items_stay_in_backend(self):
        damaged = [
            {key: value for key, value in ATTRIBUTES.items() if key != "username"},
            ATTRIBUTES | {"origin": ORIGIN + "/login"},
            ATTRIBUTES | {"origin": ORIGIN + ":443"},
            ATTRIBUTES | {"application": "example"},
            ATTRIBUTES | {"schema": "2"},
        ]
        for attrs in damaged:
            with self.subTest(attrs=attrs):
                self.backend.put(attrs, TYPED, "")
                self.assertEqual(self.store.list(), [])
                self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN))
                self.assertEqual(len(self.backend.entries), 1)
                self.backend.entries.clear()
        self.assertEqual(self.backend.reads, [])
        self.assertEqual(self.backend.deletes, [])

    def test_detached_store(self):
        store = Store()
        for operation in (store.list, lambda: store.remove(NAME),
                          lambda: store.add(NAME, ORIGIN, USER, TYPED),
                          lambda: store.secret_for(NAME, ORIGIN)):
            self.safe_error(operation, "No login keyring is attached.")


class KeyringCheck(LoginCheck):
    def setUp(self):
        self.module = ModuleType("secretstorage")
        self.util = ModuleType("secretstorage.util")
        self.module.exceptions = SimpleNamespace(LockedException=type("LockedException", (Exception,), {}))
        self.connection = Mock()
        self.collection = Mock(connection=self.connection)
        self.item = Mock(item_path="/example/login")
        self.item.get_attributes.return_value = ATTRIBUTES.copy()
        self.item.get_secret.return_value = TYPED.encode("utf-8")
        self.item.is_locked.return_value = False
        self.item._item.call.return_value = ("/",)
        self.collection.search_items.return_value = [self.item]
        self.collection.is_locked.return_value = False
        self.collection._collection.call.return_value = (self.item.item_path, "/")
        self.module.dbus_init = Mock(return_value=self.connection)
        self.module.Collection = Mock(return_value=self.collection)
        self.module.Item = Mock(return_value=self.item)
        self.module.get_default_collection = Mock(side_effect=AssertionError("collection creation"))
        self.util.SS_PREFIX = "org.freedesktop.Secret."
        self.util.open_session = Mock()
        self.util.format_secret = Mock()
        self.util.exec_prompt = Mock(side_effect=AssertionError("prompt"))
        self.enterContext(patch.dict(sys.modules, {"secretstorage": self.module, "secretstorage.util": self.util}))
        self.store = Store.system()

    def test_locked_refuses_every_backend_operation_without_unlock(self):
        backend = self.store._backend
        self.collection.is_locked.return_value = True
        for operation in (backend.items, lambda: backend.put(ATTRIBUTES, TYPED, ""),
                          lambda: backend.secret(None), lambda: backend.delete(None)):
            self.safe_error(operation, "The login keyring is locked. Unlock it and try again.")
        self.assertEqual(self.connection.close.call_count, 4)
        self.collection.unlock.assert_not_called()
        self.collection._collection.call.assert_not_called()
        self.collection.search_items.assert_not_called()
        self.module.Item.assert_not_called()
        self.module.get_default_collection.assert_not_called()

    def test_missing_library(self):
        with patch.dict(sys.modules, {"secretstorage": None}):
            self.safe_error(self.store.list, "The keyring library is not installed. Run: uv pip install -e .")

    def test_missing_collection_bus_or_service(self):
        self.module.Collection.side_effect = RuntimeError("missing collection or service")
        self.safe_error(self.store.list, "No login keyring is available.")
        self.connection.close.assert_called_once_with()
        self.connection.close.reset_mock()
        self.module.dbus_init.side_effect = OSError("bus unavailable")
        self.safe_error(self.store.list, "No login keyring is available.")
        self.connection.close.assert_not_called()

    def test_write_utf8_and_close_connections(self):
        self.collection.search_items.return_value = []
        self.store.add(NAME, ORIGIN, USER, TYPED)
        self.util.format_secret.assert_called_once_with(self.util.open_session.return_value,
                                                       TYPED.encode("utf-8"), "text/plain")
        self.collection._collection.call.assert_called_once_with(
            "CreateItem", "a{sv}(oayays)b", {
                "org.freedesktop.Secret.Item.Label": ("s", "Jarvis login: example"),
                "org.freedesktop.Secret.Item.Attributes": ("a{ss}", ATTRIBUTES),
            }, self.util.format_secret.return_value, False,
        )
        self.assertEqual(self.connection.close.call_count, 2)
        self.collection.create_item.assert_not_called()

    def test_reads_and_deletes_reopen_the_same_item(self):
        self.assertEqual(self.store.list(), [Login(NAME, ORIGIN, USER, "chrome")])
        self.item.get_secret.assert_not_called()
        self.assertEqual(self.store.secret_for(NAME, ORIGIN)[1], TYPED)
        self.assertTrue(self.store.remove(NAME))
        self.assertEqual(self.module.Item.call_args_list, [call(self.connection, self.item.item_path)] * 2)
        self.item._item.call.assert_called_once_with("Delete", "")
        self.item.delete.assert_not_called()
        self.assertEqual(self.connection.close.call_count, 5)

    def test_changed_attributes_do_not_return_a_secret(self):
        changed = ATTRIBUTES | {"origin": "https://other.example.invalid"}
        self.item.get_attributes.side_effect = [ATTRIBUTES, changed]
        self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN), "The saved login changed. Try again.")
        self.item.get_secret.assert_not_called()
        self.item.get_attributes.side_effect = [ATTRIBUTES, ATTRIBUTES, changed]
        self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN), "The saved login changed. Try again.")
        self.item.get_secret.assert_called_once_with()

    def test_backend_errors_hide_library_messages(self):
        self.collection._collection.call.side_effect = OSError(TYPED)
        error = self.safe_error(lambda: self.store._backend.put(ATTRIBUTES, TYPED, ""),
                                "No login keyring is available.")
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        frames = traceback.TracebackException.from_exception(error, capture_locals=True)
        self.assertNotIn("fake-password", "".join(frames.format()))
        self.item.get_secret.side_effect = OSError(TYPED)
        error = self.safe_error(lambda: self.store.secret_for(NAME, ORIGIN), "No login keyring is available.")
        self.assertIsNone(error.__context__)
        self.assertEqual(self.connection.close.call_count, 3)

    def test_prompt_replies_are_refused_without_executing_them(self):
        self.collection._collection.call.return_value = ("/", "/example/prompt")
        self.item._item.call.return_value = ("/example/prompt",)
        for operation in (lambda: self.store._backend.put(ATTRIBUTES, TYPED, ""),
                          lambda: self.store.remove(NAME)):
            self.safe_error(operation, "The login keyring requires interaction. Complete it outside Jarvis and try again.")
        self.util.exec_prompt.assert_not_called()
        self.collection.create_item.assert_not_called()
        self.item.delete.assert_not_called()
        self.item.unlock.assert_not_called()
        self.collection.unlock.assert_not_called()
        self.assertEqual(self.connection.close.call_count, 3)

    def test_import_does_not_load_secretstorage(self):
        subprocess.run(
            [sys.executable, "-c", "import sys; import jarvis.v3.logins; "
             "assert 'secretstorage' not in sys.modules"],
            cwd=Path(__file__).resolve().parents[2], timeout=10, check=True,
        )


class CliCheck(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend()
        self.store = Store(self.backend)
        self.factory = self.enterContext(patch.object(cli, "_login_store", return_value=self.store))
        self.getpass = self.enterContext(patch("getpass.getpass"))
        self.isatty = self.enterContext(patch.object(sys.stdin, "isatty", return_value=True))
        self.add = ("add", NAME, "--site", ORIGIN, "--user", USER)

    def run_login(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["login", *args])
        self.assertNotIn("fake-password", out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_nonterminal_refused_before_keyring_or_prompt(self):
        self.isatty.return_value = False
        self.assertEqual(self.run_login(*self.add), (
            1, "", "Error: Run this in your own terminal. "
                   "The password is typed there and is never passed as an argument.\n",
        ))
        self.factory.assert_not_called()
        self.getpass.assert_not_called()

    def test_package_entry_point_refuses_nonterminal(self):
        result = subprocess.run(
            [sys.executable, "-m", "jarvis", "login", *self.add], input="",
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Error: Run this in your own terminal. "
                                        "The password is typed there and is never passed as an argument.\n")

    def test_validation_and_clash_before_prompt(self):
        for args in (("add", "BAD", "--site", ORIGIN, "--user", USER),
                     (*self.add, "--site", ORIGIN + "/page"),
                     (*self.add, "--user", ""),
                     (*self.add, "--browser", "firefox")):
            code, out, err = self.run_login(*args)
            self.assertEqual((code, out), (1, ""))
            self.assertTrue(err.startswith("Error: "))
        self.store.add(NAME, ORIGIN, USER, TYPED)
        self.assertEqual(self.run_login(*self.add), (
            1, "", "Error: A saved login named example already exists. Use --replace to replace it.\n",
        ))
        self.getpass.assert_not_called()

    def test_mismatch_empty_and_unavailable_hidden_input(self):
        for replies, error in (((TYPED, "another"), "The passwords did not match."),
                               (("", ""), "The password cannot be empty."),
                               ((TYPED, ""), "The password cannot be empty.")):
            self.getpass.side_effect = replies
            self.assertEqual(self.run_login(*self.add), (1, "", f"Error: {error}\n"))
            self.assertEqual(self.backend.entries, {})

        def echo_fallback(_prompt):
            warnings.warn("Cannot disable echo", getpass.GetPassWarning)
            self.fail("Echo fallback continued")

        self.getpass.side_effect = echo_fallback
        self.assertEqual(self.run_login(*self.add),
                         (1, "", "Error: Could not read the password without terminal echo.\n"))
        self.assertEqual(self.backend.entries, {})

    def test_add_list_and_remove(self):
        self.assertEqual(self.run_login("list"), (0, "No saved logins.\n", ""))
        self.getpass.side_effect = [TYPED, TYPED]
        self.assertEqual(self.run_login(*self.add, "--browser", "edge"), (
            0, f"Saved login {NAME} for {ORIGIN} (user {USER}, browser edge).\n", "",
        ))
        self.assertEqual(self.getpass.call_args_list,
                         [call(f"Password for {USER} at {ORIGIN}: "), call("Again: ")])
        self.assertEqual(self.store.secret_for(NAME, ORIGIN)[1], TYPED)
        self.store.add("alpha", "https://example.invalid", "u@example.com", TYPED)
        self.assertEqual(self.run_login("list"), (
            0, f"{'alpha':7}  {'https://example.invalid':32}  {'u@example.com':16}  chrome\n"
               f"{NAME}  {ORIGIN}  {USER}  edge\n", "",
        ))
        self.assertEqual(self.run_login("remove", NAME), (0, "Removed login example.\n", ""))
        self.assertEqual(self.run_login("remove", NAME), (1, "", "Error: No saved login named example.\n"))


if __name__ == "__main__":
    unittest.main()
