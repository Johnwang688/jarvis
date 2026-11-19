"""Chrome profile-picker adapter: trusted identity, card reading, one bounded choice.

Linux-only and free: fake windows, fake UIA trees, fake clock. The real picker
is exercised by the opt-in tests/voice_chrome_picker_real.py.
"""
import json
import queue
import threading
from types import SimpleNamespace
import unittest

from windows import chromepicker as cp
from windows import voiceactions as actions
from windows.chromepicker import BUTTON, DOCUMENT, EDIT, GROUP, PANE, Card, WindowIdentity
from windows.voiceactions import LaunchStatus

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PICKER = WindowIdentity(10, 77, "Chrome_WidgetWin_1", CHROME, "Chrome.SystemProfile", "Google Chrome", True)


def window(hwnd, title="New Tab - Google Chrome", aumid="Chrome.Profile1", pid=77, **kw):
    fields = dict(hwnd=hwnd, pid=pid, class_name="Chrome_WidgetWin_1", exe=CHROME, aumid=aumid,
                  title=title, visible=True)
    fields.update(kw)
    return WindowIdentity(**fields)


def card(label, key=None, name=None, **kw):
    fields = dict(label=label, button_name=f"Open {label} profile" if name is None else name,
                  key=key or (42, hash(label) & 0xFFFF), enabled=True, offscreen=False, invokable=True)
    fields.update(kw)
    return Card(**fields)


class Predicates(unittest.TestCase):
    def test_picker_aumid_is_the_system_profile_of_stable_chrome_only(self):
        for good in ("Chrome.SystemProfile", "Chrome.jarvispickerproai.SystemProfile"):
            self.assertTrue(cp.is_picker_aumid(good), good)
        for bad in (None, "", "Chrome", "Chrome.Profile1", "ChromeBeta.SystemProfile", "Chrome.a.b.SystemProfile",
                    "Chrome..SystemProfile", "Chrome.SystemProfile.", "chrome.systemprofile", "Chrome.Sys Profile",
                    "Chrome.Ŝ.SystemProfile", "MSEdge.SystemProfile", "Chrome." + "x" * 130 + ".SystemProfile", 7):
            self.assertFalse(cp.is_picker_aumid(bad), bad)

    def test_picker_identity_needs_process_class_visibility_and_aumid_not_title(self):
        self.assertTrue(cp.is_picker_identity(PICKER))
        for change in (dict(exe=r"C:\x\msedge.exe"), dict(exe=r"C:\x\notchrome.exe"), dict(exe=""),
                       dict(class_name="Chrome_WidgetWin_0"), dict(visible=False), dict(aumid=None),
                       dict(aumid="Chrome"), dict(aumid="Chrome.Profile1")):
            with self.subTest(change=change):
                self.assertFalse(cp.is_picker_identity(PICKER.__class__(**{**PICKER.__dict__, **change})))
        # A browser tab titled like the picker is still a browser window.
        self.assertFalse(cp.is_picker_identity(window(11, title="Google Chrome", aumid="Chrome")))
        self.assertFalse(cp.is_picker_identity(("10", "Google Chrome", CHROME)))

    def test_same_picker_requires_the_recorded_handle_process_and_identity(self):
        self.assertTrue(cp.same_picker(PICKER, PICKER))
        for change in (dict(hwnd=11), dict(pid=78), dict(aumid="Chrome.other.SystemProfile"), dict(visible=False)):
            self.assertFalse(cp.same_picker(PICKER.__class__(**{**PICKER.__dict__, **change}), PICKER))
        self.assertFalse(cp.same_picker(None, PICKER))

    def test_profile_window_is_new_same_process_same_install_and_a_browser(self):
        self.assertTrue(cp.is_profile_window(window(20), PICKER, frozenset({10})))
        self.assertTrue(cp.is_profile_window(window(20, aumid="Chrome"), PICKER, frozenset()))
        custom = window(10, aumid="Chrome.jarvispickerpro8u.SystemProfile", title="Google Chrome")
        # Measured: Chrome shortens the directory component differently per window.
        self.assertTrue(cp.is_profile_window(window(20, aumid="Chrome.jarvispickerpron46xg8u.Profile1"), custom, frozenset()))
        self.assertTrue(cp.is_profile_window(window(20, aumid="Chrome.jarvispickerproxx"), custom, frozenset()))
        for bad, picker, before in (
                (window(20, pid=78), PICKER, frozenset()),
                (window(20), PICKER, frozenset({20})),
                (window(10), PICKER, frozenset()),
                (window(20, aumid="Chrome.SystemProfile"), PICKER, frozenset()),
                (window(20, aumid="Chrome.x.Profile1"), PICKER, frozenset()),
                (window(20, aumid="Chrome"), custom, frozenset()),
                (window(20, aumid="Chrome.a.b.Profile1"), custom, frozenset()),
                (window(20, aumid="Chrome.x.SystemProfile"), custom, frozenset()),
                (window(20, aumid="ChromeBeta.Profile1"), PICKER, frozenset()),
                (window(20, aumid="MSEdge.Profile1"), PICKER, frozenset()),
                (window(20, aumid=None), PICKER, frozenset()),
                (window(20, title="Google Chrome"), PICKER, frozenset()),
                (window(20, exe=r"C:\x\msedge.exe"), PICKER, frozenset()),
                (window(20, visible=False), PICKER, frozenset()),
                (window(20), window(10, aumid="Chrome"), frozenset())):
            with self.subTest(bad=bad, picker=picker.aumid):
                self.assertFalse(cp.is_profile_window(bad, picker, before))

    def test_cards_need_consistent_visible_invokable_labels(self):
        self.assertTrue(cp.card_ok(card("Work")))
        for bad in (card("Work", name="Open Personal profile"), card("Work", invokable=False),
                    card("Work", enabled=False), card("Work", offscreen=True), card("", name="Open  profile"),
                    card("Wo\u202erk"), card("Work\u0007"), card("x" * 81), card("Work", name="Open Workshop profile")):
            self.assertFalse(cp.card_ok(bad), bad)

    def test_duplicates_are_shown_but_never_chosen(self):
        cards = [card("Work", key=(1,)), card("WORK", key=(2,)), card("Personal", key=(3,))]
        self.assertEqual(cp.options(cards), ("Work", "WORK", "Personal"))
        self.assertIsNone(cp.unique_card(cards, "work"))
        self.assertEqual(cp.unique_card(cards, "Personal.").key, (3,))
        self.assertIsNone(cp.unique_card(cards, "Pers"))
        self.assertIsNone(cp.unique_card(cards, "  "))
        # An unusable card cannot make a duplicate unique by dropping out silently... nor be chosen.
        self.assertIsNone(cp.unique_card([card("Work", enabled=False)], "Work"))


class Tree:
    """Dict tree: node = dict(props..., kids=[...], value=...)."""

    def children(self, node):
        return node.get("kids", [])

    def props(self, node):
        base = dict(control=GROUP, name="", aid="", cls="", fw="Chrome", enabled=True, offscreen=False,
                    invoke=False, key=(id(node),))
        base.update({k: v for k, v in node.items() if k not in ("kids", "value")})
        return base

    def value(self, node):
        return node.get("value")


def n(**kw):
    return dict(kw)


def card_box(label, name=None, extra_button=False):
    button = n(control=BUTTON, aid="profileCardButton", name=name or f"Open {label} profile", invoke=True)
    edit = n(control=EDIT, aid="input", name="Rename your profile", value=label)
    kids = [button, n(aid="profileNameInputWrapper", kids=[n(aid="nameInput", kids=[edit])]),
            n(control=BUTTON, aid="moreActionsButton", name=f"More actions for {label}", invoke=True)]
    if extra_button:
        kids.append(dict(button))
    return n(aid="profileCardContainer", kids=kids)


def picker_tree(boxes, url="chrome://profile-picker/", *, forged_view=False, two_views=False, main_extra=()):
    main = n(aid="mainView", kids=[n(control=GROUP, aid="addProfile"), *boxes, *main_extra])
    doc = n(control=DOCUMENT, aid="RootWebArea", value=url, kids=[main])
    host = n(control=PANE, cls="Chrome_RenderWidgetHostHWND", fw="Win32", kids=[doc])
    view = n(control=PANE, cls="ProfilePickerView", kids=[host])
    client = n(control=PANE, cls="ClientView", kids=[view] + ([dict(view)] if two_views else []))
    if forged_view:
        # A page that names an element ProfilePickerView sits under web content.
        fake_view = n(control=PANE, cls="ProfilePickerView", kids=[host])
        page = n(control=DOCUMENT, aid="RootWebArea", value="https://evil.test/",
                 kids=[fake_view])
        tab_host = n(control=PANE, cls="Chrome_RenderWidgetHostHWND", fw="Win32", kids=[page])
        client = n(control=PANE, cls="ClientView", kids=[tab_host])
    frame = n(control=PANE, cls="NativeFrameView", kids=[client])
    return n(control=50032, cls="Chrome_WidgetWin_1", fw="Win32",
             kids=[n(control=PANE, cls="RootView", kids=[n(control=PANE, cls="NonClientView", kids=[frame])])])


class TreeReading(unittest.TestCase):
    def test_genuine_picker_lists_cards_in_order(self):
        cards = cp.read_cards(Tree(), picker_tree([card_box("Alpha Probe"), card_box("Beta Probe")]))
        self.assertEqual(cp.options(cards), ("Alpha Probe", "Beta Probe"))
        self.assertTrue(all(c.button_name.startswith("Open ") for c in cards))

    def test_forged_or_unfamiliar_structures_are_not_the_picker(self):
        tree = Tree()
        for label, root in (
                ("forged view under web content", picker_tree([card_box("A")], forged_view=True)),
                ("wrong url", picker_tree([card_box("A")], url="https://accounts.google.com/")),
                ("different internal page", picker_tree([card_box("A")], url="chrome://profile-picker-other/")),
                ("no url", picker_tree([card_box("A")], url=None)),
                ("two views", picker_tree([card_box("A")], two_views=True)),
                ("duplicate button in card", picker_tree([card_box("A", extra_button=True)])),
                ("two main views", picker_tree([card_box("A")], main_extra=())),
        ):
            if label == "two main views":
                doc = root["kids"][0]["kids"][0]["kids"][0]["kids"][0]["kids"][0]["kids"][0]["kids"][0]
                doc["kids"].append(dict(doc["kids"][0]))
            with self.subTest(label):
                self.assertIsNone(cp.read_cards(tree, root))

    def test_card_without_name_field_fails_the_whole_read(self):
        box = card_box("A")
        box["kids"] = [k for k in box["kids"] if k.get("aid") != "profileNameInputWrapper"]
        self.assertIsNone(cp.read_cards(Tree(), picker_tree([card_box("B"), box])))

    def test_rename_in_progress_card_is_listed_out(self):
        cards = cp.read_cards(Tree(), picker_tree([card_box("Typing", name="Open Work profile"), card_box("Home")]))
        self.assertEqual(cp.options(cards), ("Home",))

    def test_too_many_cards_is_refused(self):
        self.assertIsNone(cp.read_cards(Tree(), picker_tree([card_box(f"P{i}") for i in range(cp.MAX_CARDS + 1)])))

    def test_empty_picker_is_an_empty_list_not_none(self):
        self.assertEqual(cp.read_cards(Tree(), picker_tree([])), [])


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Backend:
    """Fake Windows picker adapter with scripted transitions."""

    def __init__(self, clock, cards=None):
        self.clock = clock
        self.windows = {PICKER.hwnd: PICKER, 5: window(5, title="Old - Google Chrome", aumid="Chrome")}
        self.fg = PICKER.hwnd
        self.card_list = cards if cards is not None else [card("Personal", key=(1,)), card("Work", key=(2,))]
        self.invoked, self.reads = [], 0
        self.on_invoke = self.open_profile_window
        self.on_read = None
        self.fg_script = None

    def open_profile_window(self, chosen):
        del self.windows[PICKER.hwnd]
        self.windows[30] = window(30, title="New Tab - Google Chrome", aumid="Chrome.Profile1")
        self.fg = 30

    def identity(self, hwnd):
        return self.windows.get(hwnd)

    def foreground(self):
        if self.fg_script:
            return self.fg_script.pop(0)
        return self.fg

    def process_windows(self, pid):
        return frozenset(h for h, w in self.windows.items() if w.pid == pid)

    def cards(self, identity):
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        return list(self.card_list)

    def invoke(self, chosen):
        self.invoked.append(chosen.label)
        if self.on_invoke:
            self.on_invoke(chosen)


class PickerDesktop:
    def __init__(self, backend):
        self.backend, self.launched = backend, False

    def foreground_window(self):
        if not self.launched:
            return None
        w = self.backend.windows.get(self.backend.fg)
        return (w.hwnd, w.title, w.exe) if w else None


class Choose(unittest.TestCase):
    def make(self, cards=None):
        self.clock = Clock()
        self.backend = Backend(self.clock, cards)
        self.desktop = PickerDesktop(self.backend)
        index = json.dumps([{"Name": "Google Chrome", "AppID": "chrome-id"}])
        a = actions.Actions(self.desktop, self.clock, self.clock.sleep,
                            lambda *a, **k: SimpleNamespace(returncode=0, stdout=index),
                            lambda argv, **kw: setattr(self.desktop, "launched", True), picker=self.backend)
        self.assertEqual(a.open_app("Google Chrome", exact=True), LaunchStatus.CHROME_PROFILE_REQUIRED)
        return a

    def test_options_and_choice_launch_only_after_a_real_transition(self):
        a = self.make()
        self.assertEqual(a.chrome_profile_options(), ("Personal", "Work"))
        self.assertEqual(a.choose_chrome_profile("work."), LaunchStatus.LAUNCHED)
        self.assertEqual(self.backend.invoked, ["Work"])
        # The picker is spent: no second press, no stale options.
        self.assertEqual(a.chrome_profile_options(), ())
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, ["Work"])

    def test_no_picker_adapter_or_untrusted_window_never_reads_or_presses(self):
        a = self.make()
        a.picker_context = None
        self.assertEqual(a.chrome_profile_options(), ())
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual((self.backend.invoked, self.backend.reads), ([], 0))
        # A title-only "Google Chrome" window from a browser profile is not remembered.
        clock, backend = Clock(), Backend(Clock())
        backend.windows[PICKER.hwnd] = window(PICKER.hwnd, title="Google Chrome", aumid="Chrome")
        desktop = PickerDesktop(backend)
        index = json.dumps([{"Name": "Google Chrome", "AppID": "chrome-id"}])
        b = actions.Actions(desktop, clock, clock.sleep, lambda *x, **k: SimpleNamespace(returncode=0, stdout=index),
                            lambda argv, **kw: setattr(desktop, "launched", True), picker=backend)
        self.assertEqual(b.open_app("chrome"), LaunchStatus.CHROME_PROFILE_REQUIRED)
        self.assertIsNone(b.picker_context)
        self.assertEqual(b.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(backend.invoked, [])
        # Test doubles without an adapter (old launch checks) keep working.
        c = actions.Actions(desktop, clock, clock.sleep, lambda *x, **k: SimpleNamespace(returncode=0, stdout=index),
                            lambda argv, **kw: None)
        self.assertEqual(c.chrome_profile_options(), ())

    def test_ambiguous_or_unknown_names_press_nothing_and_keep_waiting(self):
        a = self.make([card("Work", key=(1,)), card("WORK", key=(2,)), card("Home", key=(3,))])
        self.assertEqual(a.chrome_profile_options(), ("Work", "WORK", "Home"))
        for said in ("Work", "work", "Office", "", "Hom"):
            self.assertIn(a.choose_chrome_profile(said),
                          (LaunchStatus.CHROME_PROFILE_REQUIRED,), said)
        self.assertEqual(self.backend.invoked, [])
        self.assertEqual(a.choose_chrome_profile("Home"), LaunchStatus.LAUNCHED)

    def test_focus_elsewhere_before_or_during_revalidation_presses_nothing(self):
        a = self.make()
        self.backend.fg = 5  # owner clicked another window
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.reads, 0)  # a background picker is not even read
        self.backend.fg = None
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.backend.fg = PICKER.hwnd
        self.backend.fg_script = [PICKER.hwnd, 5]  # moves between the read and the press
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, [])
        self.backend.fg_script = [PICKER.hwnd, PICKER.hwnd, 5]  # moves during the final read
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, [])
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.LAUNCHED)  # still the same picker

    def test_card_changing_between_read_and_press_is_not_pressed(self):
        a = self.make()

        def rerender(reads):
            if reads == 2:
                self.backend.card_list = [card("Personal", key=(1,)), card("Work", key=(9,))]
        self.backend.on_read = rerender
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, [])

        def duplicate(reads):
            self.backend.card_list = [card("Work", key=(2,)), card("work", key=(7,))]
        self.backend.on_read = duplicate
        self.backend.card_list = [card("Work", key=(2,))]
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_PROFILE_REQUIRED)
        self.assertEqual(self.backend.invoked, [])

    def test_closed_or_replaced_picker_is_forgotten(self):
        for replacement in (None, window(PICKER.hwnd, pid=88, aumid="Chrome.SystemProfile", title="Google Chrome"),
                            window(PICKER.hwnd, aumid="Chrome.Profile1")):
            with self.subTest(replacement=replacement):
                a = self.make()
                if replacement is None:
                    del self.backend.windows[PICKER.hwnd]
                else:
                    self.backend.windows[PICKER.hwnd] = replacement
                self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
                self.assertIsNone(a.picker_context)
                self.assertEqual(self.backend.invoked, [])

    def test_picker_replaced_after_revalidation_read_is_not_pressed(self):
        a = self.make()

        def swap(reads):
            if reads == 1:
                self.backend.windows[PICKER.hwnd] = PICKER.__class__(**{**PICKER.__dict__, "pid": 99})
        self.backend.on_read = swap
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, [])

    def test_no_false_confirmation(self):
        cases = {
            "picker stays open": lambda c: None,
            "picker closes, old window focused": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                                            setattr(self.backend, "fg", 5)),
            "picker closes, nothing focused": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                                         setattr(self.backend, "fg", None)),
            "new window in another process": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                                        self.backend.windows.__setitem__(31, window(31, pid=12)),
                                                        setattr(self.backend, "fg", 31)),
            "new system-profile window": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                                    self.backend.windows.__setitem__(
                                                        31, window(31, aumid="Chrome.SystemProfile")),
                                                    setattr(self.backend, "fg", 31)),
            "new window but picker still open": lambda c: (self.backend.windows.__setitem__(31, window(31)),
                                                           setattr(self.backend, "fg", 31)),
            "new window without a tab title": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                                         self.backend.windows.__setitem__(
                                                             31, window(31, title="Google Chrome")),
                                                         setattr(self.backend, "fg", 31)),
            "focus to another app": lambda c: (self.backend.windows.__setitem__(
                4242, window(4242, pid=12, exe=r"C:\\Windows\\notepad.exe", class_name="Notepad")),
                setattr(self.backend, "fg", 4242)),
            "foreground vanished": lambda c: (self.backend.windows.pop(PICKER.hwnd),
                                              setattr(self.backend, "fg", 4343)),
        }
        for label, effect in cases.items():
            with self.subTest(label):
                a = self.make()
                self.backend.on_invoke = effect
                start = self.clock.now
                self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
                self.assertEqual(self.backend.invoked, ["Work"])
                self.assertLessEqual(self.clock.now - start, a.PICKER_CONFIRM_WAIT + a.PICKER_READ_WAIT + 0.5)
                if label == "focus to another app":
                    self.assertLess(self.clock.now - start, 0.5)  # stops at once, no waiting out the clock
                self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
                self.assertEqual(self.backend.invoked, ["Work"])  # no retry

    def test_transient_foreground_does_not_end_the_wait(self):
        a = self.make()

        def flicker(chosen):
            self.backend.windows.pop(PICKER.hwnd)
            self.backend.windows[30] = window(30)
            self.backend.fg_script = [4343, None, 4343]  # gone before it could be read
            self.backend.fg = 30
        self.backend.on_invoke = flicker
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.LAUNCHED)

    def test_invoke_error_never_confirms_an_independent_window_transition(self):
        a = self.make()

        def raise_after_acting(chosen):
            self.backend.open_profile_window(chosen)
            raise cp.PickerUnavailable("timed out")
        self.backend.on_invoke = raise_after_acting
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        a = self.make()

        def raise_only(chosen):
            raise OSError("gone")
        self.backend.on_invoke = raise_only
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)

    def test_slow_accessibility_tree_is_read_again_within_a_bound(self):
        a = self.make()
        lists = [None, [], [card("Work", key=(2,))]]
        self.backend.cards = lambda identity: lists.pop(0) if lists else [card("Work", key=(2,))]
        self.assertEqual(a.chrome_profile_options(), ("Work",))
        a = self.make()
        self.backend.cards = lambda identity: None
        start = self.clock.now
        self.assertEqual(a.chrome_profile_options(), ())
        self.assertLessEqual(self.clock.now - start, a.PICKER_READ_WAIT + 0.2)
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_PROFILE_REQUIRED)

        def broken(identity):
            raise cp.PickerUnavailable("stuck")
        self.backend.cards = broken
        self.assertEqual(a.chrome_profile_options(), ())

    def test_context_expires_and_a_new_launch_resets_it(self):
        a = self.make()
        self.clock.now += a.PICKER_TTL + 1
        self.assertEqual(a.choose_chrome_profile("Work"), LaunchStatus.CHROME_WINDOW_UNCONFIRMED)
        self.assertEqual(self.backend.invoked, [])
        a = self.make()
        self.backend.windows[PICKER.hwnd] = window(PICKER.hwnd, title="Google Chrome", aumid="Chrome")
        a.open_app("Google Chrome")  # this launch met an ordinary window, not the trusted picker
        self.assertIsNone(a.picker_context)


class Worker(unittest.TestCase):
    def test_stuck_call_is_abandoned_and_threads_are_capped(self):
        worker = cp._UiaWorker()
        release = threading.Event()
        self.addCleanup(release.set)

        def wait_then_answer(jobs, ready, box):
            ready.set()
            release.wait(5)
            while (job := jobs.get()) is not None:
                fn, result, done = job
                result["value"] = fn(None, None)
                done.set()

        worker._run = wait_then_answer
        self.addCleanup(worker.close)
        for _ in range(worker.MAX_THREADS):
            with self.assertRaisesRegex(cp.PickerUnavailable, "timed out"):
                worker.call(lambda *_: 1, 0.02)
        with self.assertRaisesRegex(cp.PickerUnavailable, "stopped answering"):
            worker.call(lambda *_: 1, 0.02)
        release.set()
        for thread in worker.threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        generation, result = worker.call(lambda *_: 7, 1)
        self.assertEqual(generation, worker.MAX_THREADS + 1)
        self.assertEqual(result, 7)
        worker.close()
        with self.assertRaises(cp.PickerUnavailable):
            worker.call(lambda *_: 1, 1)

    def test_card_from_an_abandoned_uia_thread_is_never_pressed(self):
        picker = object.__new__(cp.WindowsChromePicker)
        picker.worker, picker.invoke_timeout = cp._UiaWorker(), 1
        picker.worker.started, picker.worker.queue = 2, None  # the reading thread was abandoned
        with self.assertRaises(cp.PickerUnavailable):
            picker.invoke(card("Work", element=(1, object())))
        picker.worker.queue = queue.Queue()
        with self.assertRaises(cp.PickerUnavailable):
            picker.invoke(card("Work", element=(1, object())))  # a newer thread started since
        self.assertTrue(picker.worker.queue.empty())  # nothing was sent to press

    def test_failed_start_reports_unavailable(self):
        worker = cp._UiaWorker(thread_setup=lambda: (_ for _ in ()).throw(OSError("no desktop")))
        with self.assertRaises(cp.PickerUnavailable):
            worker.call(lambda client, uia: 1, 1)

    def test_module_imports_without_windows(self):
        self.assertFalse(hasattr(cp, "ctypes"))
        self.assertEqual(cp.normal("  Work. "), "work")
        self.assertEqual(cp.clean_label("  A \n B "), "A B")


if __name__ == "__main__":
    unittest.main()
