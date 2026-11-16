r"""Owner-started Windows voice agent: Right Ctrl typing and wake questions.

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
    from .voicewire import Client, HotkeyMachine, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
    from .voicetype import Typer
    from .voiceactions import Actions
    from .voiceflow import Flow
    from . import voiceui, voicewake
else:
    from voicewire import Client, HotkeyMachine, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
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

VK_RCONTROL = 0xA3
RATE = 16000
MAX_FRAMES = 120 * RATE


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
                            stream.write(data)
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
        from ctypes import wintypes

        self.host, self.port, self.key_path = host, port, key_path
        self.done = threading.Event()
        self.online = threading.Event()
        self.recording = threading.Event()
        self.flow_running = threading.Event()
        self.clips = queue.Queue(maxsize=4)
        self.generation = 0
        self.generation_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.capture = None
        self.chunks = []
        self.frames = 0
        self.audio_bad = False
        self.user32 = ctypes.windll.user32
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = voiceui.Overlay(self.root)
        self.player = Player()
        self.wake = None

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

    def hud_in_front(self):
        import ctypes
        title = ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(self.user32.GetForegroundWindow(), title, len(title))
        return "J.A.R.V.I.S." in title.value.upper()

    def enqueue(self, wav, source, generation):
        if hasattr(self, "online") and not self.online.is_set():
            self.show(generation, "Jarvis isn't running", True)
            return
        try:
            self.clips.put_nowait((generation, wav, source))
        except queue.Full:
            self.show(generation, "Jarvis is busy — try again", True)

    def on_wake(self, wav):
        if self.done.is_set() or self.busy() or self.hud_in_front():
            return
        generation = self.next_generation()
        self.enqueue(wav, "wake", generation)

    def wake_state(self, state, text=None):
        if not self.busy() and not self.hud_in_front():
            self.overlay.set_state(state, text)

    def audio(self, indata, frames, timing, status):
        with self.capture_lock:
            if status:
                self.audio_bad = True
            count = min(frames, MAX_FRAMES - self.frames)
            if count > 0:
                pcm = indata[:count].tobytes()
                self.chunks.append(pcm)
                self.frames += count
            else:
                pcm = b""
        if pcm and hasattr(self, "overlay"):
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            self.overlay.push_level(float(np.sqrt(np.mean(samples * samples))))

    def start_capture(self):
        import sounddevice as sd
        self.recording.set()
        with self.capture_lock:
            self.chunks, self.frames, self.audio_bad = [], 0, False
        wasapi = next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)
        if wasapi is None or wasapi["default_input_device"] < 0:
            raise RuntimeError("Microphone unavailable")
        self.capture = sd.InputStream(
            device=wasapi["default_input_device"], samplerate=RATE, channels=1,
            dtype="int16", extra_settings=sd.WasapiSettings(auto_convert=True), callback=self.audio)
        self.capture.start()
        self.overlay.set_state("listening")

    def finish_capture(self, cancel=False):
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
        if cancel:
            self.show(self.generation, None)
        elif bad:
            self.show(self.generation, "Microphone capture failed", True)
        elif chunks:
            self.show(self.generation, "Transcribing…")
            wav = wav_bytes(b"".join(chunks))
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
        others = [k for k in range(1, 255) if k not in (VK_RCONTROL, 0x11)]
        previous = False
        try:
            while not self.done.is_set():
                held = bool(self.user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000)
                if held and not previous:
                    self.player.stop()  # barge-in even on a Ctrl chord
                previous = held
                other = any(self.user32.GetAsyncKeyState(k) & 0x8000 for k in others)
                for action in machine.feed(time.monotonic_ns() // 1_000_000, held, other):
                    try:
                        if action == "start":
                            self.next_generation()
                            self.start_capture()
                        else:
                            self.finish_capture(cancel=action == "cancel")
                    except Exception:
                        machine.state = "wait" if held else "idle"
                        try:
                            self.finish_capture(cancel=True)
                        except Exception:
                            pass
                        self.show(self.generation, "Microphone unavailable", True)
                self.done.wait(0.015)
        finally:
            self.finish_capture(cancel=True)

    def discard_clips(self, message):
        try:
            while True:
                item = self.clips.get_nowait()
                self.show(item[0], message, True)
        except queue.Empty:
            pass

    def network(self):
        client = None
        delay, last_ping = 1.0, 0.0
        # These objects and every Client call belong to this one worker.
        typer = Typer() if hasattr(self, "overlay") else None
        actions = Actions() if typer is not None else None
        try:
            while not self.done.is_set():
                if client is None:
                    try:
                        client = Client(self.host, self.port, load_key(self.key_path), timeout=90.0)
                        client.connect()
                        delay, last_ping = 1.0, time.monotonic()
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
                try:
                    item = self.clips.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_ping >= 3:
                        try:
                            client.ping()
                            last_ping = time.monotonic()
                        except LinkError:
                            if hasattr(self, "online"):
                                self.online.clear()
                            client.close()
                            client = None
                            self.discard_clips("Jarvis isn't running")
                    continue
                generation, clip = item[:2]
                source = item[2] if len(item) > 2 else "hotkey"
                try:
                    if typer is None:  # VA-1 headless recorder compatibility
                        self.show(generation, client.dictate(clip)["text"] or "No speech heard", True)
                    else:
                        if source == "wake" and (self.hud_in_front() or self.recording.is_set()):
                            continue
                        self.flow_running.set()
                        # Toggle-stop can enqueue while Right Ctrl is still down.
                        # Let the physical modifier up before any synthetic typing.
                        while self.user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000:
                            if self.done.wait(0.015):
                                break
                        if self.done.is_set():
                            break
                        self.player.begin()
                        flow = Flow(client, typer, actions, _FlowUI(self, generation), self.player)
                        flow.on_clip(clip, source)
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
        threading.Thread(target=self.poll, name="voice-hotkey", daemon=True).start()
        threading.Thread(target=self.network, name="voice-network", daemon=True).start()
        try:
            self.wake = voicewake.WakeListener(self.on_wake, self.overlay.push_level,
                                              self.busy, on_state=self.wake_state)
            if not self.wake.start():
                self.overlay.set_state("error", self.wake.reason)
        except Exception:
            self.overlay.set_state("error", "Wake word unavailable; Right Ctrl still works.")
        try:
            self.root.mainloop()
        finally:
            self.done.set()
            self.player.stop()
            if self.wake:
                self.wake.stop()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8409
    key = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(sys.executable).resolve().parents[2] / "voice-link.key"
    Agent(host, port, key).run()


if __name__ == "__main__":
    main()
