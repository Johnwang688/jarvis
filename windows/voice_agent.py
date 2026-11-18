r"""Owner-started Windows voice agent: system-wide dictation and wake questions.

Audio, transcripts and answers stay in memory. Start with:
venv\Scripts\python.exe voice_agent.py 127.0.0.1 8409 <key path>
"""
from __future__ import annotations

import io
from pathlib import Path
import queue
import sys
import threading
import time
import wave

if __package__:
    from .voicewire import Capture, Client, HotkeyMachine, MicrophoneRelay, new_nonce, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
    from .voicetype import Typer
    from .voiceactions import Actions
    from .voiceflow import Flow
    from . import voiceui, voicewake
else:
    from voicewire import Capture, Client, HotkeyMachine, MicrophoneRelay, new_nonce, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
    try:
        from voicetype import Typer
        from voiceactions import Actions
        from voiceflow import Flow
        import voiceui
        import voicewake
    except ModuleNotFoundError:
        # Also support importlib loading by the Linux VA-1 verifier.
        from windows.voicetype import Typer
        from windows.voiceactions import Actions
        from windows.voiceflow import Flow
        from windows import voiceui, voicewake

VK_CONTROL, VK_LCONTROL, VK_RCONTROL, VK_D = 0x11, 0xA2, 0xA3, ord("D")

# GetAsyncKeyState also exposes device and IME signals (and both Ctrl aliases).
# Only an ordinary shortcut key held together with Ctrl can cancel dictation.
SHORTCUT_KEYS = (
    0x08, 0x09, 0x0D, 0x1B, *range(0x20, 0x29), 0x2C, 0x2D, 0x2E,
    *range(ord("0"), ord("9") + 1), *range(ord("A"), ord("Z") + 1),
    *range(0x60, 0x70),  # numpad and arithmetic
    *range(0x70, 0x7C),  # F1–F12
    *range(0xBA, 0xC1), *range(0xDB, 0xE0), 0xE2,  # layout-specific punctuation
)
CTRL_D_OTHER_KEYS = tuple(key for key in SHORTCUT_KEYS if key != VK_D)
# AltGr, Shift+Ctrl+D and Windows shortcuts must not start global Ctrl+D.
EXTRA_MODIFIERS = (0x10, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA4, 0xA5)


class DesktopTalkKey:
    """Own Ctrl+D on this worker's message queue while another app has focus."""

    HOTKEY_ID = 0x4A56
    WM_HOTKEY = 0x0312
    MODS = 0x4002  # MOD_NOREPEAT | MOD_CONTROL

    def __init__(self, user32, collision):
        import ctypes
        from ctypes import wintypes as w

        self.api = user32
        self.collision = collision
        self.registered = False
        self.retry = 0.0
        self.hud = False
        self.message = w.MSG()
        self.title = ctypes.create_unicode_buffer(256)
        self.window_class = ctypes.create_unicode_buffer(64)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = w.HWND
        user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        user32.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]

    def outside_hud(self):
        hwnd = self.api.GetForegroundWindow()
        if not hwnd:
            return False
        # HUD app mode and ordinary Chrome/Edge/Firefox tabs use the same
        # stable document title. Recheck the title even for an unchanged HWND
        # because switching browser tabs does not switch top-level windows.
        if not self.api.GetClassNameW(hwnd, self.window_class, len(self.window_class)):
            return False
        if self.window_class.value not in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
            return True
        if not self.api.GetWindowTextW(hwnd, self.title, len(self.title)):
            return False
        title = self.title.value.casefold()
        return not (title == "j.a.r.v.i.s." or title.startswith(("j.a.r.v.i.s. - ", "j.a.r.v.i.s. — ")))

    def poll(self):
        import ctypes

        self.hud = not self.outside_hud()
        if self.hud:
            self.close()
        elif not self.registered and time.monotonic() >= self.retry:
            self.registered = bool(self.api.RegisterHotKey(None, self.HOTKEY_ID, self.MODS, VK_D))
            if self.registered:
                self.retry = 0.0
            else:
                if not self.retry:
                    self.collision()
                self.retry = time.monotonic() + 5.0
        pressed = False
        while self.api.PeekMessageW(ctypes.byref(self.message), None, self.WM_HOTKEY, self.WM_HOTKEY, 1):
            if self.registered and self.message.wParam == self.HOTKEY_ID:
                pressed = True
        return pressed

    def close(self):
        if self.registered:
            self.api.UnregisterHotKey(None, self.HOTKEY_ID)
            self.registered = False

RATE = 16000
MAX_FRAMES = 120 * RATE


def first_signal_frame(data, width, channels):
    """First frame exceeding -50 dBFS, ignoring leading PCM silence."""
    threshold = (1 << (width * 8 - 1)) * 0.00316
    for pos in range(0, len(data), width):
        value = int.from_bytes(data[pos:pos + width], "little", signed=width != 1)
        if abs(value - (128 if width == 1 else 0)) >= threshold:
            return pos // (width * channels)
    return None


class Player:
    """Blocking playback on the worker; stop is callable from the hotkey thread.

    A stop also suppresses a pending say reply until the next flow begins.
    Raw streams keep playback independent of sounddevice's convenience globals.
    """
    def __init__(self):
        self.playing = threading.Event()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.stream = None
        self.on_output = None

    def begin(self):
        self.cancelled.clear()

    def play(self, wav):
        if self.cancelled.is_set():
            return
        import sounddevice as sd
        self.playing.set()
        try:
            with wave.open(io.BytesIO(wav), "rb") as audio:
                dtype = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}.get(audio.getsampwidth())
                if dtype is None or audio.getcomptype() != "NONE":
                    raise RuntimeError("Unsupported speech audio")
                with sd.RawOutputStream(samplerate=audio.getframerate(), channels=audio.getnchannels(),
                                        dtype=dtype) as stream:
                    with self.lock:
                        self.stream = stream
                    try:
                        while not self.cancelled.is_set():
                            data = audio.readframes(max(1, audio.getframerate() // 20))
                            if not data:
                                break
                            start = time.monotonic()
                            stream.write(data)
                            if self.on_output and not self.cancelled.is_set():
                                frame = first_signal_frame(data, audio.getsampwidth(), audio.getnchannels())
                                if frame is not None:
                                    callback, self.on_output = self.on_output, None
                                    callback(start + stream.latency + frame / audio.getframerate())
                    except sd.PortAudioError:
                        if not self.cancelled.is_set():
                            raise
                    finally:
                        with self.lock:
                            self.stream = None
        except (wave.Error, EOFError, sd.PortAudioError):
            if not self.cancelled.is_set():
                raise RuntimeError("Speech playback unavailable") from None
        finally:
            self.playing.clear()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            if self.stream is not None:
                try:
                    self.stream.abort()
                except Exception:
                    pass


class _FlowUI:
    def __init__(self, agent, generation):
        self.agent, self.generation = agent, generation

    def set_state(self, state, text=None):
        if self.generation == self.agent.generation:
            self.agent.overlay.set_state(state, text)


class Agent:
    def __init__(self, host, port, key_path):
        import ctypes
        import tkinter as tk

        self.host, self.port, self.key_path = host, port, key_path
        self.done = threading.Event()
        self.instance = new_nonce()
        self.stop_requested = threading.Event()
        self.mic_released = threading.Event()
        self.relay = None
        self.online = threading.Event()
        self.recording = threading.Event()
        self.flow_running = threading.Event()
        self.clips = queue.Queue(maxsize=4)
        self.generation = 0
        self.generation_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.capture_online = False
        self.packet_ready = False
        self.upload = None
        self.client = None
        self.capture = None
        self.tapped = False
        self.chunks = []
        self.frames = 0
        self.audio_bad = False
        self.user32 = ctypes.windll.user32
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = voiceui.Overlay(self.root, recording=self.recording.is_set)
        self.desktop_key = DesktopTalkKey(self.user32, lambda: self.overlay.set_state(
            "error", "Ctrl+D is in use by another app. Use Right Ctrl to dictate."))
        self.player = Player()
        self.wake = None

    def share_frame(self, pcm):
        relay = self.relay
        if relay is not None:
            relay.offer(pcm)

    def next_generation(self):
        with self.generation_lock:
            self.generation += 1
            return self.generation

    def show(self, generation, text, transient=False):
        # Keep the VA-1 headless recorder seam usable without creating Win32/Tk.
        if not hasattr(self, "overlay"):
            self.events.put((generation, text, transient))
        elif generation == self.generation:
            self.overlay.set_state("hidden" if text is None else "error" if transient else "transcribing", text)

    def busy(self):
        return self.recording.is_set() or self.player.playing.is_set() or self.flow_running.is_set()

    def wake_status(self):
        wake = getattr(self, "wake", None)
        if wake is None:
            return "starting"
        return wake.status()

    def enqueue(self, wav, source, generation, *, speech_end=None):
        if hasattr(self, "online") and not self.online.is_set():
            self.show(generation, "Jarvis isn't running", True)
            return
        try:
            self.clips.put_nowait((generation, wav, source, speech_end) if speech_end is not None else (generation, wav, source))
        except queue.Full:
            self.show(generation, "Jarvis is busy — try again", True)

    def on_wake(self, wav):
        if self.done.is_set() or self.busy():
            return
        generation = self.next_generation()
        self.enqueue(wav, "wake", generation, speech_end=getattr(getattr(self, "wake", None), "speech_end", None))

    def wake_state(self, state, text=None):
        if not self.busy():
            self.overlay.set_state(state, text)

    def audio(self, indata, frames, timing, status):
        with self.capture_lock:
            if status:
                self.audio_bad = True
            count = min(frames, MAX_FRAMES - self.frames)
            if count > 0:
                pcm = indata[:count].tobytes()
                upload = getattr(self, "upload", None)
                if upload is None:
                    self.chunks.append(pcm)
                else:
                    upload.append(pcm)
                self.frames += count
            else:
                pcm = b""
        if pcm and hasattr(self, "overlay"):
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            level = float(np.sqrt(np.mean(samples * samples)))
            if upload is not None and level > 0.01:
                upload.speech_end = time.monotonic()
            self.overlay.push_level(level)

    def start_capture(self):
        import sounddevice as sd
        self.recording.set()
        with self.capture_lock:
            self.chunks, self.frames, self.audio_bad = [], 0, False
            self.capture_online = not hasattr(self, "online") or self.online.is_set()
            if getattr(self, "packet_ready", False) and self.capture_online:
                self.upload = Capture()
                try:
                    self.clips.put_nowait((self.generation, self.upload, "hotkey"))
                except queue.Full:
                    self.upload = None
        # A second stream on the mic fails to start while the wake listener's
        # runs, so borrow that one when it is live.
        wake = getattr(self, "wake", None)
        if wake is not None and wake.tap(self.audio):
            self.tapped = True
            self.overlay.set_state("listening")
            return
        wasapi = next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)
        if wasapi is None or wasapi["default_input_device"] < 0:
            raise RuntimeError("Microphone unavailable")
        self.capture = sd.InputStream(
            device=wasapi["default_input_device"], samplerate=RATE, channels=1,
            dtype="int16", extra_settings=sd.WasapiSettings(auto_convert=True), callback=self.audio)
        self.capture.start()
        self.overlay.set_state("listening")

    def finish_capture(self, cancel=False):
        release_at = time.monotonic()
        if getattr(self, "tapped", False):
            self.tapped = False
            self.wake.tap(None)
        capture, self.capture = self.capture, None
        try:
            if capture:
                try:
                    capture.stop()
                finally:
                    capture.close()
        finally:
            if hasattr(self, "recording"):
                self.recording.clear()
        with self.capture_lock:
            chunks, self.chunks = self.chunks, []
            bad = self.audio_bad
        wav = wav_bytes(b"".join(chunks)) if chunks and not cancel and not bad else None
        with self.capture_lock:
            # discard_clips invalidates a capture even if the link recovers
            # before release. Keep the check and enqueue atomic with discard.
            online = not hasattr(self, "online") or self.online.is_set()
            valid = (not hasattr(self, "online")
                     or (getattr(self, "capture_online", False) and online))
            self.capture_online = False
            upload = getattr(self, "upload", None)
            if upload is not None:
                self.upload = None
                commit = valid and not cancel and not bad and self.frames > 0
                if commit:
                    self.show(self.generation, "Transcribing…")
                upload.finish(cancel=not commit, release_at=release_at)
                if commit:
                    return
            if cancel:
                self.show(self.generation, None)
            elif not valid:
                self.show(self.generation, "Jarvis isn't running", True)
            elif bad:
                self.show(self.generation, "Microphone capture failed", True)
            elif wav:
                self.show(self.generation, "Transcribing…")
                if hasattr(self, "overlay"):
                    self.enqueue(wav, "hotkey", self.generation)
                else:
                    try:
                        self.clips.put_nowait((self.generation, wav))
                    except queue.Full:
                        self.show(self.generation, "Jarvis is busy — try again", True)
            else:
                self.show(self.generation, "No speech heard", True)

    def poll(self):
        machine = HotkeyMachine()
        desktop = getattr(self, "desktop_key", None)
        ctrl_d = block_right = desktop_capture = False
        previous = False
        def down(vk):
            return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)
        try:
            while not self.done.is_set():
                press = desktop.poll() if desktop is not None else False
                right = down(VK_RCONTROL)
                if not right:
                    block_right = False
                if press:
                    ctrl_d = True
                    if machine.state == "hold":
                        desktop_capture = True  # Right Ctrl joined by D
                if ctrl_d:
                    # A fast tap can already be up by the time WM_HOTKEY is
                    # dequeued. Give the machine one down edge in that case.
                    held = press or (down(VK_D) and down(VK_CONTROL))
                    others = CTRL_D_OTHER_KEYS
                    if not held:
                        ctrl_d = False
                        block_right = right
                else:
                    held = right and not block_right
                    others = SHORTCUT_KEYS
                # Sample chords only during physical overlap. A key first seen
                # on release belongs to the foreground app's next gesture.
                shortcut = next((k for k in others if down(k)), None) if held else None
                other = shortcut is not None or (held and not previous and any(down(k) for k in EXTRA_MODIFIERS))
                if desktop_capture and desktop.hud and machine.state in ("hold", "toggle"):
                    # A desktop gesture cannot finish as dictation into the HUD.
                    self.finish_capture(cancel=True)
                    machine.state = "wait" if held else "idle"
                    desktop_capture = False
                if held and not previous and not other:
                    self.player.stop()
                    client = getattr(self, "client", None)
                    if client is not None:
                        client.interrupt_speech()
                if other and held and not previous and machine.state == "toggle":
                    # A shortcut pressed while toggle-recording is not a stop tap.
                    machine.held = True
                previous = held
                for action in machine.feed(time.monotonic_ns() // 1_000_000, held, other):
                    try:
                        if action == "start":
                            desktop_capture = ctrl_d
                            self.next_generation()
                            self.start_capture()
                        else:
                            desktop_capture = False
                            self.finish_capture(cancel=action == "cancel")
                            if action == "cancel" and not (desktop is not None and desktop.hud and down(VK_D)):
                                label = (f"Ctrl+{chr(shortcut)} shortcut" if shortcut is not None
                                         and (ord("0") <= shortcut <= ord("9") or ord("A") <= shortcut <= ord("Z"))
                                         else "Keyboard shortcut")
                                self.show(self.generation, f"{label} pressed. Dictation canceled.", True)
                    except Exception:
                        machine.state = "wait" if held else "idle"
                        try:
                            self.finish_capture(cancel=True)
                        except Exception:
                            pass
                        self.show(self.generation, "Microphone unavailable", True)
                self.done.wait(0.015)
        finally:
            if desktop is not None:
                desktop.close()
            self.finish_capture(cancel=True)

    def discard_clips(self, message):
        with self.capture_lock:
            self.capture_online = False
            self.packet_ready = False
            if getattr(self, "upload", None) is not None:
                self.upload.finish(cancel=True)
            try:
                while True:
                    item = self.clips.get_nowait()
                    self.show(item[0], message, True)
            except queue.Empty:
                pass

    def network(self):
        client = None
        delay, last_ping, reported_wake = 1.0, 0.0, None
        # These objects and every Client call belong to this one worker.
        typer = Typer() if hasattr(self, "overlay") else None
        actions = Actions(warm_in_background=True) if typer is not None else None
        try:
            while not self.done.is_set():
                if client is None:
                    if getattr(self, "relay", None) is not None:
                        self.relay.close()
                        self.relay = None
                    try:
                        client = Client(self.host, self.port, load_key(self.key_path), timeout=90.0)
                        client.connect()
                        delay, last_ping, reported_wake = 1.0, 0.0, None
                        if hasattr(self, "online"):
                            # Drop anything captured during the failed/offline interval.
                            self.discard_clips("Jarvis isn't running")
                            self.online.set()
                    except (KeyFileError, LinkError) as exc:
                        if client:
                            client.close()
                        client = None
                        message = ("Not paired: run jarvis voice-link setup" if isinstance(exc, KeyFileError)
                                   else "That wasn't Jarvis — refused" if isinstance(exc, NotJarvis)
                                   else "Jarvis isn't running")
                        self.discard_clips(message)
                        self.done.wait(delay)
                        delay = min(30.0, delay * 2)
                        continue
                wake = self.wake_status() if typer is not None else None
                if wake != reported_wake or time.monotonic() - last_ping >= 3:
                    try:
                        if wake is None:
                            client.ping()
                        else:
                            if hasattr(self, "instance"):
                                client.ping(wake=wake, instance=self.instance)
                                if client.shared_capable and self.relay is None:
                                    self.relay = MicrophoneRelay(client, self.wake_status,
                                                                 self.stop_requested, self.mic_released)
                                    self.relay.start()
                            else:
                                client.ping(wake=wake)
                        self.client = client
                        self.packet_ready = client.streaming is True
                        last_ping, reported_wake = time.monotonic(), wake
                    except LinkError:
                        if hasattr(self, "online"):
                            self.online.clear()
                        client.close()
                        client = None
                        self.discard_clips("Jarvis isn't running")
                        continue
                try:
                    item = self.clips.get(timeout=0.2)
                except queue.Empty:
                    continue
                generation, clip = item[:2]
                source = item[2] if len(item) > 2 else "hotkey"
                try:
                    if typer is None:  # VA-1 headless recorder compatibility
                        self.show(generation, client.dictate(clip)["text"] or "No speech heard", True)
                    else:
                        if source == "wake" and self.recording.is_set():
                            continue
                        self.flow_running.set()
                        transcript = None
                        capture = clip if isinstance(clip, Capture) else None
                        if capture is not None:
                            got = client.dictate_capture(capture)
                            if got is None:
                                continue
                            transcript = got["text"]
                        # Toggle-stop can enqueue while either Ctrl key is still down.
                        # Let the physical modifier up before any synthetic typing.
                        while self.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
                            if self.done.wait(0.015):
                                break
                        if self.done.is_set():
                            break
                        if generation == self.generation:
                            self.player.begin()
                        flow = Flow(client, typer, actions, _FlowUI(self, generation), self.player)
                        speech_end = capture.speech_end if capture else item[3] if len(item) > 3 else None
                        def metric(label, start, end):
                            if start is not None and generation == self.generation:
                                self.overlay.timing(label, max(0, round(1000 * (end - start))))
                        self.player.on_output = lambda end: metric("Voice end to estimated audio", speech_end, end)
                        typer.on_insert = lambda end: metric("Capture stop to text input", capture.release_at if capture else None, end)
                        if transcript is None:
                            flow.on_clip(clip, source)
                        else:
                            flow.on_clip(None, source, transcript=transcript)
                        if flow.link_error is not None:
                            raise flow.link_error
                    last_ping = time.monotonic()
                except LinkError as exc:
                    self.show(generation, str(exc), True)
                    if hasattr(self, "online"):
                        self.online.clear()
                    client.close()
                    client = None
                    self.discard_clips("Jarvis isn't running")
                finally:
                    if hasattr(self, "flow_running"):
                        self.flow_running.clear()
                    clip = item = None
        finally:
            if hasattr(self, "online"):
                self.online.clear()
            if client:
                client.close()
            if actions:
                actions.close()

    def run(self):
        hotkey = threading.Thread(target=self.poll, name="voice-hotkey", daemon=True)
        hotkey.start()
        threading.Thread(target=self.network, name="voice-network", daemon=True).start()
        try:
            self.wake = voicewake.WakeListener(self.on_wake, self.overlay.push_level,
                                              self.busy, on_state=self.wake_state)
            self.wake.on_pcm = self.share_frame
            if not self.wake.start():
                self.overlay.set_state("error", self.wake.reason)
        except Exception:
            self.overlay.set_state("error", "Wake word unavailable; Right Ctrl still works.")
        def check_stop():
            if self.stop_requested.is_set():
                self.root.quit()
            else:
                self.root.after(100, check_stop)
        if hasattr(self, "stop_requested"):
            self.root.after(100, check_stop)
        try:
            self.root.mainloop()
        finally:
            self.done.set()
            self.player.stop()
            if self.wake:
                self.wake.stop()
            hotkey.join()
            if hasattr(self, "mic_released"):
                self.mic_released.set()
                relay = self.relay
                if relay:
                    relay.close()
                    relay.thread.join(2)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8409
    key = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(sys.executable).resolve().parents[2] / "voice-link.key"
    # One helper per port in this logged-in Windows session, including launches
    # from a shortcut during a WSL reconnect. Windows releases this on exit.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, f"Local\\JarvisVoiceAnywhere-{port}")
    if not handle:
        raise OSError("Could not open the Windows voice session.")
    try:
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return
        agent = Agent(host, port, key)
        if len(sys.argv) > 4 and len(sys.argv[4]) == 64:
            agent.instance = sys.argv[4]
        agent.run()
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
