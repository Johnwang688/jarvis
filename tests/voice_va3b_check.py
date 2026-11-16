"""VA-3b pure Windows flow checks; no Windows APIs, devices or model calls."""
from types import SimpleNamespace
import ctypes
import unittest
from unittest.mock import Mock

from windows.voiceflow import Flow
from windows.voicetype import Typer, WindowsInput, app_category
from windows.voicewire import Client, LinkError


class Backend:
    def __init__(self):
        self.hwnd, self.exe, self.events = 1, 'notepad.exe', []
    def foreground(self):
        return self.hwnd
    def foreground_exe(self):
        return self.exe
    def text(self, text):
        self.events.append(('text', text))
    def key(self, key):
        self.events.append(('key', key))


class CleanupFlow(unittest.TestCase):
    def make(self, text, routed=False):
        self.client, self.ui = Mock(), Mock()
        self.client.dictate.return_value = {'text': text}
        self.client.clean.side_effect = lambda t, app: {'text': t.upper(), 'pass': 'rules'}
        self.client.route.return_value = {'kind': 'dictate', 'text': 'clean me send it'}
        self.backend = Backend()
        self.typer = Typer(self.backend, clock=lambda: 10)
        self.flow = Flow(self.client, self.typer, Mock(), self.ui, Mock())
        self.flow.on_clip(b'wav', 'hotkey')

    def test_text_over_the_daemon_cap_is_typed_raw_without_a_clean_call(self):
        long_text = 'word ' * 450  # over 2000 characters, no command words
        self.make(long_text)
        self.client.clean.assert_not_called()
        self.assertIsNone(self.flow.link_error)
        self.assertEqual(self.backend.events[0][0], 'text')

    def test_plain_and_routed_only_clean_text(self):
        for text, raw in [('hello there send it', 'hello there'), ('Jarvis type this', 'clean me')]:
            with self.subTest(text=text):
                self.make(text)
                self.client.clean.assert_called_once_with(raw, 'other')
                self.assertEqual(self.backend.events, [('text', raw.upper()), ('key', 'enter')])
        for text, events in [('new line', [('key', 'shift+enter')]), ('scratch that', [])]:
            self.make(text)
            self.client.clean.assert_not_called()
            self.assertEqual(self.backend.events, events)

    def test_errors_preserve_raw_and_link_error_without_displaying_text(self):
        for error in [LinkError('private words'), OSError('private words'), ValueError('private words')]:
            self.make('first clip')
            self.backend.events.clear()
            self.client.clean.side_effect = error
            self.flow.on_clip(b'wav', 'hotkey')
            self.assertEqual(self.backend.events, [('text', 'first clip')])
            self.assertIs(self.flow.link_error, error if isinstance(error, LinkError) else None)
            self.ui.set_state.assert_called_with('done')
            self.assertNotIn('private words', str(self.ui.mock_calls))

    def test_scratch_reports_last_pass_once_only_when_deleting(self):
        self.make('hello')
        self.client.dictate.return_value = {'text': 'scratch that'}
        self.flow.on_clip(b'wav', 'hotkey')
        self.client.scratch.assert_called_once_with('rules')
        self.flow.on_clip(b'wav', 'hotkey')
        self.client.scratch.assert_called_once()
        self.assertEqual(self.backend.events.count(('key', 'backspace')), 5)

    def test_scratch_failure_ignored_and_window_age_guards_remain(self):
        for expired, moved in [(False, False), (True, False), (False, True)]:
            self.make('hello')
            self.client.scratch.side_effect = RuntimeError('private words')
            if expired:
                self.typer.clock = lambda: 70
            if moved:
                self.backend.hwnd = 2
            self.client.dictate.return_value = {'text': 'scratch that'}
            self.flow.on_clip(b'wav', 'hotkey')
            self.assertEqual(self.client.scratch.call_count, int(not expired and not moved))
            self.ui.set_state.assert_called_with('done')

    def test_off_types_exact_raw_and_category_is_read_for_each_clip(self):
        self.make('hello')
        self.client.clean.side_effect = lambda t, app: {'text': t, 'pass': 'off'}
        self.client.dictate.return_value = {'text': 'um keep MY casing send it'}
        self.backend.exe = 'Code.EXE'
        self.flow.on_clip(b'wav', 'hotkey')
        self.client.clean.assert_called_with('um keep MY casing', 'terminal')
        self.assertEqual(self.backend.events[-2:], [('text', 'um keep MY casing'), ('key', 'enter')])

    def test_malformed_cleanup_keeps_raw(self):
        for value in [None, {}, {'text': 1, 'pass': 'rules'}, {'text': 'bad', 'pass': 'unknown'}]:
            self.make('hello')
            self.client.clean.side_effect = None
            self.client.clean.return_value = value
            self.flow.on_clip(b'wav', 'hotkey')
            self.assertEqual(self.backend.events[-1], ('text', 'hello'))

    def test_fake_typer_receives_cleaned_steps_and_injected_scratch_callback(self):
        client, typer = Mock(), Mock()
        client.dictate.return_value = {'text': 'um keep this send it'}
        client.clean.return_value = {'text': 'Keep this', 'pass': 'rules'}
        typer.category.side_effect = OSError('private window title')
        flow = Flow(client, typer, Mock(), Mock(), Mock())
        flow.on_clip(b'wav', 'hotkey')
        client.clean.assert_called_once_with('um keep this', 'terminal')
        typer.run.assert_called_once_with([('text', 'Keep this', 'rules'), ('key', 'enter')])
        typer.on_scratch('rules')
        client.scratch.assert_called_once_with('rules')

    def test_wire_rejects_invalid_cleanup_replies_with_fixed_error(self):
        for reply in [{'text': 3, 'pass': 'rules'}, {'text': 'private', 'pass': 'model'},
                      {'text': 'x' * 2001, 'pass': 'off'}]:
            client = Client('unused', 0, b'key')
            client._request, client.close = Mock(return_value=reply), Mock()
            with self.assertRaisesRegex(LinkError, '^The voice link sent an invalid reply\\.$'):
                client.clean('hello', 'terminal')
            client.close.assert_called_once()


class AppCategory(unittest.TestCase):
    def test_categories(self):
        for name in [None, '', 'unknown.exe', 'powershell.EXE', r'C:\Apps\Code.EXE',
                     'Code - Insiders.exe', 'IDEA64.exe', 'wezterm-gui.exe', 'sublime_text.exe']:
            self.assertEqual(app_category(name), 'terminal', name)
        for name in ['NOTEPAD.EXE', r'C:\Apps\chrome.exe', 'firefox', 'Claude.exe', 'ChatGPT.exe', 'Notion.exe']:
            self.assertEqual(app_category(name), 'other', name)

    def test_foreground_process_api_success_and_failure_close_handles(self):
        from ctypes import wintypes as w
        backend = WindowsInput.__new__(WindowsInput)
        backend.ctypes = ctypes
        backend.user32 = SimpleNamespace(GetForegroundWindow=lambda: 42)
        def pid(hwnd, ptr):
            ctypes.cast(ptr, ctypes.POINTER(w.DWORD))[0] = 99
            return 7
        backend.user32.GetWindowThreadProcessId = pid
        close = Mock(return_value=True)
        def image(handle, flags, buf, size):
            buf.value = r'C:\Apps\Code.EXE'
            return True
        backend.kernel32 = SimpleNamespace(OpenProcess=Mock(return_value=123),
                                           QueryFullProcessImageNameW=image, CloseHandle=close)
        self.assertEqual(backend.foreground_exe(), 'Code.EXE')
        backend.kernel32.OpenProcess.assert_called_once_with(0x1000, False, 99)
        close.assert_called_once_with(123)
        for query in [Mock(return_value=False), Mock(side_effect=OSError('private'))]:
            close.reset_mock()
            backend.kernel32.QueryFullProcessImageNameW = query
            self.assertIsNone(backend.foreground_exe())
            close.assert_called_once_with(123)
        backend.kernel32.OpenProcess.return_value = None
        close.reset_mock()
        self.assertIsNone(backend.foreground_exe())
        close.assert_not_called()
        backend.kernel32.OpenProcess.return_value = 123
        backend.kernel32.QueryFullProcessImageNameW = image
        close.return_value = False
        self.assertIsNone(backend.foreground_exe())


if __name__ == '__main__':
    unittest.main(verbosity=2)
